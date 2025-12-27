import os
import json
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from moviepy import VideoFileClip
from openai import OpenAI
from supabase import create_client, Client
import yt_dlp

# --- CONFIG ---
app = FastAPI()

# ⚠️ HARDCODE KEYS FOR LOCAL TESTING (Or use os.getenv)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_URL:
    raise ValueError("❌ Missing SUPABASE_URL environment variable")
    
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- REQUEST MODEL ---
class VideoRequest(BaseModel):
    url: str

# --- HELPER: DOWNLOADER ---
def download_video(url):
    print(f"🔗 Downloading: {url}")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': 'temp_%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info.get('title', 'Unknown')

# --- HELPER: THE PIPELINE (REVISED) ---
def run_pipeline(url: str):
    if not url or url == "":
        print("❌ Received empty URL. Aborting.")
        return

    print(f"🚀 Pipeline Started for: {url}")
    
    # 1. Download
    try:
        video_path, source_title = download_video(url)
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return

    # 2. Extract Audio
    print("🔊 Extracting Audio...")
    audio_path = video_path.replace(".mp4", ".mp3")
    try:
        video = VideoFileClip(video_path)
        if video.duration < 5:
            print("❌ Video too short (likely a download error/captcha). Aborting.")
            video.close()
            return
        video.audio.write_audiofile(audio_path, logger=None)
        duration = video.duration
        video.close()
    except Exception as e:
        print(f"❌ Audio Error: {e}")
        return

    # 3. Transcribe
    print("🎙️ Transcribing...")
    with open(audio_path, "rb") as f:
        transcript = client.audio.transcriptions.create(model="whisper-1", file=f)
    transcript_text = transcript.text

    if len(transcript_text) < 20:
        print(f"❌ Transcript too short ('{transcript_text}'). Aborting.")
        return

    # 4. AI Analysis
    print("🧠 AI Analyzing...")
    
    # STRONGER PROMPT
    prompt = f"""
    Analyze this transcript. Output Valid JSON.
    
    REQUIRED JSON STRUCTURE:
    {{
      "metadata": {{
        "title": "Short Descriptive Title",
        "speaker": "Name or Unknown",
        "category": "Broad Category (e.g. Psychology)",
        "sub_category": "Specific Niche (e.g. Behavior)"
      }},
      "questions": [
        {{
          "stage": 0,
          "q": "Question text?",
          "correct": "Correct Answer",
          "wrong": ["Wrong1", "Wrong2", "Wrong3"]
        }}
      ]
    }}
    
    TRANSCRIPT: {transcript_text[:15000]}
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a database entry bot. Output ONLY valid JSON."}, 
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        # DEBUG: Print the raw AI response so we can see if it fails
        raw_content = response.choices[0].message.content
        print(f"🔍 AI RAW OUTPUT: {raw_content[:100]}...") 
        
        ai_data = json.loads(raw_content)
        meta = ai_data.get('metadata', {})
        questions = ai_data.get('questions', [])
        
    except Exception as e:
        print(f"❌ AI JSON Error: {e}")
        return

    # 5. Upload Video
    print("☁️ Uploading...")
    # Safe filename
    safe_title = "".join(x for x in meta.get('title', 'Video') if x.isalnum() or x in " _-")
    filename = f"{safe_title.replace(' ', '_')}.mp4"
    
    try:
        with open(video_path, 'rb') as f:
            supabase.storage.from_("videos").upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4", "x-upsert": "true"}
            )
        public_url = supabase.storage.from_("videos").get_public_url(filename)
    except Exception as e:
        print(f"❌ Upload Error: {e}")
        return

    # 6. Save DB
    print("💾 Saving to DB...")
    
    # PURE DATA: No merging.
    # We trust the DB columns to hold separate values.
    category_val = meta.get('category', 'General')
    sub_cat_val = meta.get('sub_category', None) # Default to None/Null if empty
    
    vid_data = {
        "video_url": public_url,
        "title": meta.get('title', 'Untitled'),
        "transcript_text": transcript_text,
        "category": category_val,       # e.g. "Psychology"
        "sub_category": sub_cat_val,    # e.g. "Media Consumption"
        "duration_seconds": int(duration)
    }
    
    try:
        res = supabase.table("content_library").insert(vid_data).execute()
        new_id = res.data[0]['id']
        
        # 7. Insert Questions
        if questions:
            q_inserts = []
            for q in questions:
                q_inserts.append({
                    "video_id": new_id,
                    "difficulty_phase": q.get('stage', 0),
                    "question_text": q.get('q', 'Error'),
                    "correct_answer": q.get('correct', 'Error'),
                    "wrong_options": q.get('wrong', [])
                })
            supabase.table("questions").insert(q_inserts).execute()
            print(f"✅ Success! Saved {len(q_inserts)} questions.")
        else:
            print("⚠️ Warning: AI returned 0 questions (Check transcript length).")
            
    except Exception as e:
        print(f"❌ Database Save Error: {e}")

    # Cleanup
    if os.path.exists(video_path): os.remove(video_path)
    if os.path.exists(audio_path): os.remove(audio_path)
    print("✅ PIPELINE COMPLETE")

# --- ENDPOINT ---
@app.post("/capture")
async def capture_video(request: VideoRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_pipeline, request.url)
    return {"status": "processing", "message": "Fluency is curating this video..."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)