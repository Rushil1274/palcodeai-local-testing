import os
import io
import re
import json
import uuid
import time
import shutil
import base64
import phonenumbers
import datetime as dt
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form, Depends, HTTPException, Header, Request, Query
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import httpx
from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, create_engine, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func
from sqlalchemy.exc import OperationalError

from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument

# OpenAI SDK (>=1.0)
from openai import OpenAI
import jwt  # PyJWT

# ============== ENV & CONST =================
load_dotenv()

API_KEY = os.getenv("API_KEY", "devkey")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ?? NEW: Development mode flag
DEVELOPMENT_MODE = os.getenv("DEVELOPMENT_MODE", "false").lower() == "true"

VONAGE_API_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_API_SECRET = os.getenv("VONAGE_API_SECRET")
VONAGE_APPLICATION_ID = os.getenv("VONAGE_APPLICATION_ID")
VONAGE_PRIVATE_KEY_PATH = os.getenv("VONAGE_PRIVATE_KEY_PATH")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

OUTBOUND_WHITELIST = set(
    [n.strip() for n in os.getenv("OUTBOUND_WHITELIST", "").split(",") if n.strip()]
)

RETENTION_HOURS = int(os.getenv("RETENTION_HOURS", "36"))

ARTIFACTS_DIR = os.path.abspath("./artifacts")
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

# ============== DB ===========================
Base = declarative_base()
engine = create_engine("sqlite:///./interview.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Job(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    jd_text = Column(Text)
    questions = Column(JSON)
    created_at = Column(DateTime, server_default=func.now())

class Candidate(Base):
    __tablename__ = "candidates"
    id = Column(String, primary_key=True)
    name = Column(String)
    phone_e164 = Column(String)
    resume_meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

class Interview(Base):
    __tablename__ = "interviews"
    id = Column(String, primary_key=True)
    job_id = Column(String)
    candidate_id = Column(String)
    status = Column(String, default="created")  # created|calling|in_progress|completed|failed
    provider_call_id = Column(String, nullable=True)
    answers = Column(JSON, default=list)  # [{q_idx, question, recording_url, local_audio, transcript, score}]
    final_recommendation = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

def init_db():
    Base.metadata.create_all(bind=engine)

# ============== SECURITY =====================
def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True

# ?? NEW: Development mode helper
def is_development_mode():
    return DEVELOPMENT_MODE

# ============== APP ==========================
app = FastAPI(title="AI Interview Screener (Backend Only)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# OpenAI client
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set. Question gen/STT/Scoring will fail.")
client_oa = OpenAI(api_key=OPENAI_API_KEY)

# Retention sweeper
def sweep_artifacts():
    cutoff = time.time() - RETENTION_HOURS * 3600
    for root, _, files in os.walk(ARTIFACTS_DIR):
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass

init_db()
sweep_artifacts()

# Print startup mode
if is_development_mode():
    print("?? DEVELOPMENT MODE: Calls will be simulated (no real phone calls)")
    print("   Use POST /v1/dev/simulate-answers/{interview_id} after triggering interview")
else:
    print("?? PRODUCTION MODE: Real calls will be placed via Vonage")

# ============== MODELS (Pydantic) ============
class CandidateIn(BaseModel):
    name: str
    phone_e164: str

class JDIn(BaseModel):
    jd_text: Optional[str] = None

# new: flexible trigger payload
class InterviewTriggerIn(BaseModel):
    # Option A: send candidate_id
    candidate_id: Optional[str] = None
    # Option B: send name + phone (auto-create candidate)
    name: Optional[str] = None
    phone_e164: Optional[str] = None
    # job: explicit or auto-latest
    job_id: Optional[str] = None
    from_number: str = Field(..., description="Your Vonage E.164 number, e.g. +12025550123")

# ============== HELPERS ======================
def read_pdf(file_bytes: bytes) -> str:
    with io.BytesIO(file_bytes) as fp:
        return pdf_extract_text(fp) or ""

def read_docx(file_bytes: bytes) -> str:
    with io.BytesIO(file_bytes) as fp:
        doc = DocxDocument(fp)
    return "\n".join([p.text for p in doc.paragraphs])

def to_e164(phone: str) -> str:
    try:
        pn = phonenumbers.parse(phone, None)
        if not phonenumbers.is_possible_number(pn) or not phonenumbers.is_valid_number(pn):
            raise ValueError("Invalid")
        return phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid E.164 phone")

def ensure_whitelisted(phone_e164: str):
    if OUTBOUND_WHITELIST and (phone_e164 not in OUTBOUND_WHITELIST):
        raise HTTPException(status_code=403, detail="Outbound number not in whitelist")

def save_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def get_latest_job(session) -> Optional[Job]:
    return session.query(Job).order_by(Job.created_at.desc()).first()

async def openai_questions(jd_text: str) -> List[str]:
    prompt = f"""
You are an expert technical interviewer. From this job description, generate 5-7 concise, role-relevant phone interview questions. Avoid trivia; test applied skill and communication.

JD:
---
{jd_text}
---
Return as a numbered list only.
"""
    resp = client_oa.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"user","content":prompt}],
        temperature=0.2,
    )
    text = resp.choices[0].message.content.strip()
    qs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\d+[\).\s-]+(.*)$", line)
        qs.append((m.group(1) if m else line).strip())
    return qs[:7]

async def openai_transcribe_from_url(mp3_url: str) -> str:
    async with httpx.AsyncClient(timeout=60) as hx:
        r = await hx.get(mp3_url)
        r.raise_for_status()
        b = r.content
    tmp_path = os.path.join(ARTIFACTS_DIR, f"{uuid.uuid4().hex}.mp3")
    with open(tmp_path, "wb") as f:
        f.write(b)
    with open(tmp_path, "rb") as f:
        tr = client_oa.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )
    return tr.text.strip()

async def openai_score(questions: List[str], transcripts: List[str]) -> Dict[str, Any]:
    rubric = """Score each answer from 1-5 (5=excellent) considering relevance, clarity, correctness, and depth. Return JSON:
{
  "per_answer": [{"q_idx":0,"question":"...", "transcript":"...", "score":4, "rationale":"..."}],
  "final_score": 4.1,
  "recommendation": "Strong yes|Yes|Leaning yes|Neutral|Leaning no|No"
}"""
    user = {"questions": questions, "answers": transcripts}
    resp = client_oa.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":"You are a fair interview evaluator."},
            {"role":"user","content": f"Evaluate the following interview. {rubric}\n\n{json.dumps(user, ensure_ascii=False)}"}
        ],
        temperature=0.1,
    )
    txt = resp.choices[0].message.content.strip()
    try:
        m = re.search(r"\{.*\}", txt, flags=re.S)
        jtxt = m.group(0) if m else txt
        return json.loads(jtxt)
    except Exception:
        return {"per_answer": [], "final_score": None, "recommendation": "Neutral"}

def build_vonage_jwt(application_id: str, private_key_path: str) -> str:
    if not application_id or not private_key_path or not Path(private_key_path).exists():
        raise HTTPException(500, "Vonage app/private key not configured")
    now = int(time.time())
    payload = {
        "application_id": application_id,
        "iat": now,
        "exp": now + 60*5,
        "jti": uuid.uuid4().hex
    }
    with open(private_key_path, "r") as f:
        pk = f.read()
    token = jwt.encode(payload, pk, algorithm="RS256")
    return token

# ?? NEW: Generate fake interview answers for development
def generate_fake_answers(questions: List[str]) -> List[str]:
    """Generate realistic fake answers for development testing"""
    fake_templates = [
        "I have {years} years of experience with {skill}. In my previous role at {company}, I worked extensively on {project_type} projects using {technology}.",
        "Yes, I'm very familiar with {skill}. I've used it to {use_case} and have experience with {related_tech}. I particularly enjoy {aspect} because it allows for {benefit}.",
        "In my current position, I handle {responsibility} using {tool}. My approach involves {methodology} and I always ensure {quality_measure}.",
        "I've worked on several {project_type} projects. The most challenging one involved {challenge} where I had to {solution}. The result was {outcome}.",
        "My experience includes {skill_1}, {skill_2}, and {skill_3}. I'm particularly strong in {strength} and have been learning {learning_area}.",
    ]
    
    # Common tech terms for realistic answers
    skills = ["Python", "FastAPI", "SQL", "REST APIs", "Docker", "AWS", "Git", "testing"]
    companies = ["a fintech startup", "an e-commerce company", "a healthcare firm", "a logistics company"]
    technologies = ["microservices", "cloud infrastructure", "database optimization", "API development"]
    
    import random
    answers = []
    
    for i, question in enumerate(questions):
        template = fake_templates[i % len(fake_templates)]
        answer = template.format(
            years=random.choice(["2", "3", "4", "5"]),
            skill=random.choice(skills),
            company=random.choice(companies),
            project_type=random.choice(["backend", "full-stack", "API", "data"]),
            technology=random.choice(technologies),
            use_case=random.choice(["build scalable APIs", "optimize database queries", "implement authentication"]),
            related_tech=random.choice(["PostgreSQL", "Redis", "Kubernetes", "Jenkins"]),
            aspect=random.choice(["problem-solving", "optimization", "architecture design"]),
            benefit=random.choice(["better performance", "cleaner code", "faster deployment"]),
            responsibility=random.choice(["API development", "database design", "code reviews"]),
            tool=random.choice(["SQLAlchemy", "Pydantic", "pytest", "Docker"]),
            methodology=random.choice(["TDD", "agile practices", "code reviews", "documentation"]),
            quality_measure=random.choice(["high code coverage", "proper error handling", "security best practices"]),
            challenge=random.choice(["performance bottlenecks", "complex business logic", "scaling issues"]),
            solution=random.choice(["refactor the architecture", "implement caching", "optimize queries"]),
            outcome=random.choice(["50% performance improvement", "reduced latency", "better user experience"]),
            skill_1=random.choice(skills[:3]),
            skill_2=random.choice(skills[3:6]),
            skill_3=random.choice(skills[6:]),
            strength=random.choice(["backend development", "API design", "database optimization"]),
            learning_area=random.choice(["machine learning", "DevOps", "cloud architecture"])
        )
        answers.append(answer)
    
    return answers

# ============== ROUTES =======================
@app.get("/", response_class=PlainTextResponse)
def root():
    mode = "Development Mode (Free Testing)" if is_development_mode() else "Production Mode"
    return f"AI Interview Screener Backend OK - {mode}"

# --- JD  Questions (text or file) ---
@app.post("/v1/jd", dependencies=[Depends(require_api_key)])
async def jd_to_questions(jd_text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
    if not jd_text and not file:
        raise HTTPException(400, "Provide jd_text or file")
    if file:
        fb = await file.read()
        if file.filename.lower().endswith(".pdf"):
            jd_text = read_pdf(fb)
        elif file.filename.lower().endswith(".docx"):
            jd_text = read_docx(fb)
        else:
            jd_text = fb.decode("utf-8", errors="ignore")
    jd_text = (jd_text or "").strip()
    if not jd_text:
        raise HTTPException(400, "Empty JD")

    questions = await openai_questions(jd_text)
    job_id = uuid.uuid4().hex
    with SessionLocal() as s:
        s.add(Job(id=job_id, jd_text=jd_text, questions=questions))
        s.commit()
    return {"job_id": job_id, "questions": questions}

# --- Resume Upload & Parse ---
@app.post("/v1/resume", dependencies=[Depends(require_api_key)])
async def upload_resume(file: UploadFile = File(...)):
    fb = await file.read()
    ext = file.filename.lower()
    text = ""
    if ext.endswith(".pdf"):
        text = read_pdf(fb)
    elif ext.endswith(".docx"):
        text = read_docx(fb)
    else:
        try:
            text = fb.decode("utf-8", errors="ignore")
        except:
            text = ""

    email = None
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if m: email = m.group(0)
    phone_guess = None
    m2 = re.search(r"(\+?\d[\d\s\-()]{7,})", text)
    if m2: phone_guess = m2.group(1)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    name_guess = lines[0] if lines else "Unknown"
    skills = []
    for l in lines[:50]:
        if "skills" in l.lower():
            skills = re.split(r"[,\|/\-]+", l.split(":")[-1])
            skills = [s.strip() for s in skills if s.strip()]
            break

    meta = {
        "name_guess": name_guess,
        "email": email,
        "phone_guess": phone_guess,
        "skills": skills[:20]
    }
    resume_id = uuid.uuid4().hex
    fpath = os.path.join(ARTIFACTS_DIR, f"resume_{resume_id}.txt")
    save_bytes(fpath, text.encode("utf-8"))
    meta["artifact_path"] = fpath
    return {"resume_id": resume_id, "parsed": meta}

# --- Candidate Add ---
@app.post("/v1/candidates", dependencies=[Depends(require_api_key)])
async def add_candidate(payload: CandidateIn):
    phone = to_e164(payload.phone_e164)
    # ?? In development mode, skip whitelist check for easier testing
    if not is_development_mode():
        ensure_whitelisted(phone)
    cand = Candidate(id=uuid.uuid4().hex, name=payload.name, phone_e164=phone, resume_meta=None)
    with SessionLocal() as s:
        s.add(cand); s.commit()
    return {"candidate_id": cand.id, "name": cand.name, "phone_e164": cand.phone_e164}

# --- Attach resume meta (optional) ---
@app.post("/v1/candidates/{candidate_id}/resume_meta", dependencies=[Depends(require_api_key)])
async def attach_resume_meta(candidate_id: str, meta: Dict[str, Any]):
    with SessionLocal() as s:
        c = s.get(Candidate, candidate_id)
        if not c: raise HTTPException(404, "Candidate not found")
        c.resume_meta = meta
        s.commit()
    return {"ok": True}

# ?? MODIFIED: Trigger Interview (now with development mode)
@app.post("/v1/interviews", dependencies=[Depends(require_api_key)])
async def trigger_interview(data: InterviewTriggerIn):
    # Resolve job and candidate
    with SessionLocal() as s:
        job = s.get(Job, data.job_id) if data.job_id else get_latest_job(s)
        if not job:
            raise HTTPException(404, "No job found. Create questions first via /v1/jd.")

        if data.candidate_id:
            cand = s.get(Candidate, data.candidate_id)
            if not cand:
                raise HTTPException(404, "candidate_id invalid")
        else:
            if not (data.name and data.phone_e164):
                raise HTTPException(400, "Provide candidate_id OR name + phone_e164")
            phone = to_e164(data.phone_e164)
            if not is_development_mode():
                ensure_whitelisted(phone)
            cand = Candidate(id=uuid.uuid4().hex, name=data.name, phone_e164=phone, resume_meta=None)
            s.add(cand); s.commit()

    # Create interview record
    inter_id = uuid.uuid4().hex
    with SessionLocal() as s:
        s.add(Interview(id=inter_id, job_id=job.id, candidate_id=cand.id, status="calling", answers=[]))
        s.commit()

    # ?? DEVELOPMENT MODE: Simulate the call
    if is_development_mode():
        print(f"?? [DEV MODE] Simulating call to {cand.phone_e164} from {data.from_number}")
        print(f"?? [DEV MODE] Questions to ask: {len(job.questions)}")
        for i, q in enumerate(job.questions):
            print(f"?? [DEV MODE] Q{i+1}: {q}")
        
        # Create fake call ID and update status
        fake_call_id = f"dev_call_{uuid.uuid4().hex[:8]}"
        
        with SessionLocal() as s:
            inter = s.get(Interview, inter_id)
            inter.provider_call_id = fake_call_id
            inter.status = "in_progress"
            s.commit()
        
        return {
            "interview_id": inter_id,
            "call_uuid": fake_call_id,
            "candidate_id": cand.id,
            "job_id": job.id,
            "dev_mode": True,
            "message": "Call simulated successfully! Use POST /v1/dev/simulate-answers/{interview_id} to add test answers."
        }

    # PRODUCTION MODE: Real Vonage call (existing code)
    # Build NCCO
    record_actions = []
    for idx, q in enumerate(job.questions):
        record_actions.extend([
            {"action": "talk", "text": f"Question {idx+1}. {q}"},
            {
                "action": "record",
                "beepStart": True,
                "endOnSilence": 3,
                "format": "mp3",
                "eventUrl": [f"{PUBLIC_BASE_URL}/v1/voice/record?interview_id={inter_id}&q_idx={idx}"]
            }
        ])
    record_actions.append({"action": "talk", "text": "Thanks. This concludes the interview. Goodbye."})

    # Save NCCO to serve on /answer
    ncco_path = os.path.join(ARTIFACTS_DIR, f"ncco_{inter_id}.json")
    save_bytes(ncco_path, json.dumps(record_actions).encode("utf-8"))

    # Place call via Vonage
    jwt_token = build_vonage_jwt(
        application_id=VONAGE_APPLICATION_ID,
        private_key_path=VONAGE_PRIVATE_KEY_PATH
    )
    payload = {
        "to": [{"type":"phone", "number": cand.phone_e164}],
        "from": {"type":"phone", "number": data.from_number},
        "answer_url": [f"{PUBLIC_BASE_URL}/v1/voice/answer?n={inter_id}"],
        "event_url": [f"{PUBLIC_BASE_URL}/v1/voice/event?n={inter_id}"]
    }
    async with httpx.AsyncClient(timeout=30) as hx:
        r = await hx.post(
            "https://api.nexmo.com/v1/calls",
            headers={"Authorization": f"Bearer {jwt_token}", "Content-Type":"application/json"},
            json=payload
        )
        if r.status_code >= 300:
            with SessionLocal() as s:
                inter = s.get(Interview, inter_id)
                inter.status = "failed"
                s.commit()
            raise HTTPException(r.status_code, f"Vonage call create failed: {r.text}")
        resp_data = r.json()
        call_id = resp_data.get("uuid")

    with SessionLocal() as s:
        inter = s.get(Interview, inter_id)
        inter.provider_call_id = call_id
        inter.status = "in_progress"
        s.commit()

    return {"interview_id": inter_id, "call_uuid": call_id, "candidate_id": cand.id, "job_id": job.id}

# ?? NEW: Development endpoint to simulate interview answers
@app.post("/v1/dev/simulate-answers/{interview_id}", dependencies=[Depends(require_api_key)])
async def simulate_answers(interview_id: str):
    if not is_development_mode():
        raise HTTPException(400, "This endpoint is only available in development mode")
    
    with SessionLocal() as s:
        inter = s.get(Interview, interview_id)
        if not inter:
            raise HTTPException(404, "Interview not found")
        
        job = s.get(Job, inter.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        
        # Generate realistic fake answers
        fake_transcripts = generate_fake_answers(job.questions)
        
        # Create answer records with fake data
        answers = []
        for idx, (question, transcript) in enumerate(zip(job.questions, fake_transcripts)):
            fake_audio_url = f"https://fake-recording-service.com/interview_{interview_id}_q_{idx}.mp3"
            answers.append({
                "q_idx": idx,
                "question": question,
                "recording_url": fake_audio_url,
                "local_audio": f"/v1/dev/fake-audio/{interview_id}/q_{idx}.mp3",
                "transcript": transcript
            })
        
        # Update interview with fake answers
        inter.answers = answers
        inter.status = "completed"
        s.commit()
        
        print(f"?? [DEV MODE] Added {len(answers)} fake answers to interview {interview_id}")
        for i, answer in enumerate(answers):
            print(f"?? [DEV MODE] Q{i+1}: {answer['question'][:50]}...")
            print(f"?? [DEV MODE] A{i+1}: {answer['transcript'][:100]}...")
    
    return {
        "message": f"Successfully added {len(answers)} simulated answers",
        "interview_id": interview_id,
        "answers_count": len(answers),
        "status": "completed",
        "next_step": f"GET /v1/interviews/{interview_id} to see results with scoring"
    }

# ?? NEW: Serve fake audio files for development
@app.get("/v1/dev/fake-audio/{interview_id}/q_{q_idx}.mp3")
async def serve_fake_audio(interview_id: str, q_idx: int):
    if not is_development_mode():
        raise HTTPException(404, "Not found")
    
    # Return a simple response indicating this is fake audio
    return PlainTextResponse(
        f"[FAKE AUDIO] Interview: {interview_id}, Question: {q_idx + 1}\n"
        f"In production, this would be the actual recorded MP3 file.\n"
        f"For development, we simulate the audio file.",
        media_type="text/plain"
    )

# --- Vonage Answer webhook: return NCCO ---
@app.get("/v1/voice/answer")
async def voice_answer(n: str = Query(...)):
    ncco_path = os.path.join(ARTIFACTS_DIR, f"ncco_{n}.json")
    if not os.path.exists(ncco_path):
        return JSONResponse(content=[{"action":"talk","text":"Interview not found."}], media_type="application/json")
    with open(ncco_path, "r", encoding="utf-8") as f:
        ncco = json.load(f)
    return JSONResponse(content=ncco, media_type="application/json")

# --- Vonage Event webhook: basic status logging ---
@app.post("/v1/voice/event")
async def voice_event(n: str = Query(None), req: dict = None):
    try:
        if n and isinstance(req, dict):
            status = req.get("status")
            if status in {"completed","failed","timeout","rejected"}:
                with SessionLocal() as s:
                    inter = s.get(Interview, n)
                    if inter and inter.status != "completed":
                        inter.status = status if status != "completed" else inter.status
                        s.commit()
    except Exception:
        pass
    return PlainTextResponse("OK")

# --- Recording webhook: download audio, STT, persist ---
@app.post("/v1/voice/record")
async def voice_record(interview_id: str = Query(...), q_idx: int = Query(...), request: Request = None):
    payload = await request.json()
    rec_url = payload.get("recording_url") or payload.get("RECORDING_URL") or payload.get("url")
    if not rec_url:
        return PlainTextResponse("No recording_url", status_code=400)

    # Download MP3
    async with httpx.AsyncClient(timeout=60) as hx:
        r = await hx.get(rec_url)
        r.raise_for_status()
        audio = r.content

    # Save
    audio_rel = f"{interview_id}/q_{q_idx}.mp3"
    audio_abs = os.path.join(ARTIFACTS_DIR, audio_rel)
    os.makedirs(os.path.dirname(audio_abs), exist_ok=True)
    save_bytes(audio_abs, audio)

    # Transcribe
    try:
        transcript = await openai_transcribe_from_url(rec_url)
    except Exception as e:
        transcript = f"(transcription_failed: {e})"

    # Persist to interview
    with SessionLocal() as s:
        inter = s.get(Interview, interview_id)
        if not inter:
            return PlainTextResponse("interview not found", status_code=404)
        answers = inter.answers or []
        job = s.get(Job, inter.job_id)
        question = job.questions[q_idx] if 0 <= q_idx < len(job.questions) else f"Q{q_idx+1}"
        found = False
        for a in answers:
            if a.get("q_idx") == q_idx:
                a.update({
                    "question": question,
                    "recording_url": rec_url,
                    "local_audio": f"/v1/artifacts/{audio_rel}",
                    "transcript": transcript
                })
                found = True
                break
        if not found:
            answers.append({
                "q_idx": q_idx,
                "question": question,
                "recording_url": rec_url,
                "local_audio": f"/v1/artifacts/{audio_rel}",
                "transcript": transcript
            })
        inter.answers = answers
        s.commit()

    return PlainTextResponse("OK")

# --- Get Results (questions + transcripts + scores + recommendation) ---
@app.get("/v1/interviews/{interview_id}", dependencies=[Depends(require_api_key)])
async def get_interview(interview_id: str):
    with SessionLocal() as s:
        inter = s.get(Interview, interview_id)
        if not inter:
            raise HTTPException(404, "Not found")
        job = s.get(Job, inter.job_id)
        cand = s.get(Candidate, inter.candidate_id)

    answers_sorted = sorted(inter.answers or [], key=lambda x: x.get("q_idx", 0))
    transcripts = [a.get("transcript","") for a in answers_sorted]
    have_all = (len(answers_sorted) == len(job.questions)) and all(t for t in transcripts)

    if have_all and (not inter.final_recommendation or not any("score" in a for a in answers_sorted)):
        try:
            scoring = await openai_score([*job.questions], transcripts)
            pa = scoring.get("per_answer", [])
            by_idx = {x.get("q_idx"): x for x in pa if isinstance(x, dict) and "q_idx" in x}
            for a in answers_sorted:
                b = by_idx.get(a["q_idx"])
                if b:
                    a["score"] = b.get("score")
                    a["rationale"] = b.get("rationale")
            inter.final_recommendation = scoring.get("recommendation")
            with SessionLocal() as s2:
                i2 = s2.get(Interview, interview_id)
                i2.answers = answers_sorted
                i2.final_recommendation = inter.final_recommendation
                if i2.status not in ("completed","failed"):
                    i2.status = "completed"
                s2.commit()
        except Exception:
            pass

    # Add development mode indicator to response
    result = {
        "interview_id": inter.id,
        "status": inter.status,
        "job": {"job_id": job.id, "questions": job.questions},
        "candidate": {"candidate_id": cand.id, "name": cand.name, "phone_e164": cand.phone_e164},
        "answers": answers_sorted,
        "final_recommendation": inter.final_recommendation
    }
    
    if is_development_mode():
        result["dev_mode"] = True
        result["note"] = "This interview was conducted in development mode with simulated answers"
    
    return result

# --- Serve artifacts (audio, resumes, nccos) ---
@app.get("/v1/artifacts/{path:path}")
async def serve_artifact(path: str):
    ap = os.path.join(ARTIFACTS_DIR, path)
    if not os.path.abspath(ap).startswith(ARTIFACTS_DIR):
        raise HTTPException(403, "Invalid path")
    if not os.path.exists(ap):
        raise HTTPException(404, "Not found")
    return FileResponse(ap)

# ?? NEW: Debug helper for development mode
@app.get("/v1/dev/status", dependencies=[Depends(require_api_key)])
def dev_status():
    if not is_development_mode():
        raise HTTPException(400, "Only available in development mode")
    
    with SessionLocal() as s:
        job_count = s.query(Job).count()
        candidate_count = s.query(Candidate).count()
        interview_count = s.query(Interview).count()
        latest_job = get_latest_job(s)
        latest_candidate = s.query(Candidate).order_by(Candidate.created_at.desc()).first()
        latest_interview = s.query(Interview).order_by(Interview.created_at.desc()).first()
    
    return {
        "development_mode": True,
        "database_stats": {
            "jobs": job_count,
            "candidates": candidate_count, 
            "interviews": interview_count
        },
        "latest_records": {
            "job_id": latest_job.id if latest_job else None,
            "candidate_id": latest_candidate.id if latest_candidate else None,
            "interview_id": latest_interview.id if latest_interview else None
        },
        "openai_configured": bool(OPENAI_API_KEY),
        "vonage_required": False,
        "message": "All systems ready for development testing!"
    }

# --- Debug helper: latest job/candidate ids ---
@app.get("/v1/debug/latest", dependencies=[Depends(require_api_key)])
def debug_latest():
    with SessionLocal() as s:
        job = get_latest_job(s)
        cand = s.query(Candidate).order_by(Candidate.created_at.desc()).first()
        return {
            "latest_job_id": job.id if job else None,
            "latest_candidate_id": cand.id if cand else None,
            "development_mode": is_development_mode()
        }
