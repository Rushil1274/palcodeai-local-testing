# palcodeai-local-testing

# Complete Implementation Guide - Free Development Mode 🆓

## 📁 Files to Create/Update

### 1. `main.py` 
**Action**: Entire `main.py` with the updated version above
**Key Changes**:
- ✅ Added `DEVELOPMENT_MODE` environment variable
- ✅ Added `is_development_mode()` helper function
- ✅ Modified `trigger_interview()` to simulate calls in dev mode
- ✅ Added `POST /v1/dev/simulate-answers/{interview_id}` endpoint  
- ✅ Added realistic fake answer generation
- ✅ Added development status endpoints
- ✅ Skips phone whitelist in development mode

### 2. Create `.env` file
**Action**: Create/replace `.env` in your project root
**Content**: Use the `.env` file provided above
**Key Settings**:
```bash
DEVELOPMENT_MODE=true          # 🆓 Enables free testing
OPENAI_API_KEY=your_key_here   # Required (free tier available)
API_KEY=supersecretlocalkey    # For Postman authentication
```

### 3. Update Postman Collection
**Action**: Import the updated collection JSON
**New Features**:
- ✅ Automatic variable saving (job_id, candidate_id, interview_id)
- ✅ Development-specific endpoints
- ✅ Console logging for easy debugging
- ✅ Proper request ordering

### 4. Update Postman Environment
**Action**: Update your Postman environment with:
```json
{
  "baseUrl": "http://localhost:8000",
  "apiKey": "supersecretlocalkey",
  "fromNumber": "+919876543210"  
}
```

---

## 🚀 Step-by-Step Testing Instructions

### Step 1: Setup Project
```bash
# 1. Navigate to your project
cd ai-interview-screener

# 2. Create virtual environment (if not exists)
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# OR
.venv\Scripts\activate     # Windows

# 3. Install dependencies (if not installed)
pip install fastapi uvicorn python-dotenv httpx pydantic sqlalchemy \
            pdfminer.six python-docx phonenumbers PyJWT openai

# 4. Replace main.py with updated version (from artifact above)
# 5. Create .env file (from artifact above)
# 6. Get OpenAI API key from platform.openai.com (free tier available)
# 7. Update OPENAI_API_KEY in .env file
```

### Step 2: Start the Server
```bash
# Start FastAPI server
uvicorn main:app --reload --port 8000

# You should see:
# 🆓 DEVELOPMENT MODE: Calls will be simulated (no real phone calls)
# Server running at: http://localhost:8000
```

### Step 3: Import Postman Collection
1. Open Postman
2. **Import** → Select updated collection JSON
3. **Import** → Select environment JSON  
4. Select **"AI Interview Screener Local"** environment
5. Verify environment variables are set correctly

### Step 4: Run the Complete Flow

#### Test 1: Health Check ✅
```http
GET http://localhost:8000/
Expected: "AI Interview Screener Backend OK - Development Mode (Free Testing)"
```

#### Test 2: Generate Questions ✅  
```http
POST http://localhost:8000/v1/jd
```
**Expected Response**:
```json
{
  "job_id": "abc123...",
  "questions": [
    "Can you describe your experience with FastAPI and how you've used it in production?",
    "How do you handle database operations and what ORMs have you worked with?",
    // ... 5-7 total questions
  ]
}
```

#### Test 3: Add Candidate ✅
```http  
POST http://localhost:8000/v1/candidates
```
**Expected Response**:
```json
{
  "candidate_id": "def456...",
  "name": "Arjun Sharma", 
  "phone_e164": "+919876543210"
}
```

#### Test 4: Trigger Interview (Simulated) ✅
```http
POST http://localhost:8000/v1/interviews  
```
**Expected Response**:
```json
{
  "interview_id": "ghi789...",
  "call_uuid": "dev_call_12345678",
  "candidate_id": "def456...",
  "job_id": "abc123...",
  "dev_mode": true,
  "message": "Call simulated successfully! Use POST /v1/dev/simulate-answers/{interview_id} to add test answers."
}
```

**What happens**: 
- ✅ No real phone call placed
- ✅ Interview record created  
- ✅ Status set to "in_progress"
- ✅ Console shows simulated call details

#### Test 5: Simulate Answers ✅
```http
POST http://localhost:8000/v1/dev/simulate-answers/{interview_id}
```
**Expected Response**:
```json
{
  "message": "Successfully added 5 simulated answers",
  "interview_id": "ghi789...",
  "answers_count": 5,
  "status": "completed"
}
```

**What happens**:
- ✅ Generates realistic fake answers for each question
- ✅ Creates fake audio URLs
- ✅ Sets interview status to "completed"

#### Test 6: Get Results with Scoring ✅
```http
GET http://localhost:8000/v1/interviews/{interview_id}
```
**Expected Response**:
```json
{
  "interview_id": "ghi789...",
  "status": "completed",
  "job": {
    "job_id": "abc123...",
    "questions": ["Question 1...", "Question 2..."]
  },
  "candidate": {
    "candidate_id": "def456...", 
    "name": "Arjun Sharma",
    "phone_e164": "+919876543210"
  },
  "answers": [
    {
      "q_idx": 0,
      "question": "Can you describe your experience with FastAPI...",
      "recording_url": "https://fake-recording-service.com/...",
      "local_audio": "/v1/dev/fake-audio/ghi789.../q_0.mp3", 
      "transcript": "I have 3 years of experience with FastAPI...",
      "score": 4,
      "rationale": "Good technical understanding and clear communication..."
    }
    // ... more answers with scores
  ],
  "final_recommendation": "Yes",
  "dev_mode": true,
  "note": "This interview was conducted in development mode with simulated answers"
}
```

---

## 🎯 Expected Test Results

### ✅ Success Criteria:
- [ ] Server starts in development mode
- [ ] All 6 endpoints return 200 status
- [ ] Questions generated from JD  
- [ ] Candidate created successfully
- [ ] Interview "triggered" without real call
- [ ] Fake answers added automatically  
- [ ] Full scoring and recommendation provided
- [ ] No Vonage/phone setup required

### 🔍 Verification Points:
```bash
# Check server logs for these messages:
🆓 DEVELOPMENT MODE: Calls will be simulated
🆓 [DEV MODE] Simulating call to +919876543210
🆓 [DEV MODE] Added 5 fake answers to interview abc123
```

### 📊 Sample Output in Postman Console:
```
✅ Job ID saved: abc123def456
📝 Generated 6 questions  
✅ Candidate ID saved: def456ghi789
✅ Interview ID saved: ghi789jkl012
🆓 Development mode - call simulated!
🆓 Added 6 simulated answers
📊 Interview Status: completed
🎯 Final Recommendation: Yes
Q1 Score: 4/5 - Good technical understanding...
Q2 Score: 3/5 - Adequate response but could be more detailed...
```

---

## 🔧 Troubleshooting

### Issue 1: "OPENAI_API_KEY not set"
**Solution**: 
1. Sign up at platform.openai.com
2. Get free API key (comes with $5 credit)
3. Update `.env` file: `OPENAI_API_KEY=sk-...`

### Issue 2: "Invalid API key" (401 error)
**Solution**: 
- Check Postman environment has correct `apiKey`
- Verify `.env` has `API_KEY=supersecretlocalkey`

### Issue 3: "Development mode" not showing
**Solution**:
- Verify `.env` has `DEVELOPMENT_MODE=true`
- Restart FastAPI server to reload environment

### Issue 4: Questions not generating
**Solution**:
- Check OpenAI API key is valid and has credits
- Verify internet connection for OpenAI API calls

---

## 💰 Cost Breakdown (Development Mode)

| Component | Cost | Note |
|-----------|------|------|
| **FastAPI Server** | 🆓 Free | Local development |
| **SQLite Database** | 🆓 Free | Local file-based |
| **Voice Calls** | 🆓 Free | Simulated in dev mode |
| **OpenAI API** | ~$0.01-0.05 per interview | Question generation + scoring |
| **Total for 50 test interviews** | **~₹20-40** | Only OpenAI costs |

**Bottom Line**: You can develop and test the complete system for essentially free!

---

## 🚀 Ready for Production?

Once development testing is complete:

### Switch to Production Mode:
```bash
# Update .env
DEVELOPMENT_MODE=false

# Add real Vonage credentials
VONAGE_API_KEY=your_key
VONAGE_API_SECRET=your_secret
# ... etc
```


- Use same Postman collection (just update baseUrl)

**Your development work transfers 1:1 to production!** 🎯
