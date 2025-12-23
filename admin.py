import streamlit as st
import os
import tempfile
import json
from moviepy import VideoFileClip
from openai import OpenAI
from supabase import create_client, Client


st.set_page_config(page_title="Fluency Admin", page_icon="🧠")

# --- SIMPLE AUTHENTICATION ---
# Check if the user is already authenticated
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

def check_password():
    # We will set 'ADMIN_PASSWORD' in the cloud secrets later
    if st.session_state["password_input"] == st.secrets["ADMIN_PASSWORD"]:
        st.session_state.authenticated = True
        del st.session_state["password_input"] # clean up
    else:
        st.error("❌ Incorrect Password")

if not st.session_state.authenticated:
    st.title("🔒 Login Required")
    st.text_input("Enter Admin Password", type="password", key="password_input", on_change=check_password)
    st.stop() # Stop the script here so the rest of the app doesn't load


# --- CONFIGURATION (Load from secrets or hardcode for now) ---
# For a real web app, we'd use st.secrets, but for local run, this works:
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]

# Initialize Clients
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = OpenAI(api_key=OPENAI_API_KEY)
    st.sidebar.success("✅ Connected to Cloud")
except Exception as e:
    st.sidebar.error(f"❌ Connection Failed: {e}")

# --- HELPER FUNCTIONS ---
def process_video(uploaded_file, title, category, sub_category):
    # 1. Save to Temp File (MoviePy needs a real file path)
    tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") 
    tfile.write(uploaded_file.read())
    temp_video_path = tfile.name
    
    status_text.text("⏳ Extracting Audio...")
    progress_bar.progress(10)
    
    # 2. Extract Audio
    audio_path = "temp_audio.mp3"
    try:
        video = VideoFileClip(temp_video_path)
        video.audio.write_audiofile(audio_path, logger=None)
        duration = video.duration
        video.close()
    except Exception as e:
        st.error(f"Error processing video: {e}")
        return

    # 3. Transcribe
    status_text.text("🎙️ Transcribing with Whisper...")
    progress_bar.progress(30)
    
    with open(audio_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1", 
            file=audio_file
        )
    transcript_text = transcript.text
    
    # 4. Upload to Supabase Storage
    status_text.text("☁️ Uploading Video to Cloud...")
    progress_bar.progress(50)
    
    file_name = f"{title.replace(' ', '_')}.mp4"
    with open(temp_video_path, 'rb') as f:
        supabase.storage.from_("videos").upload(
            path=file_name,
            file=f,
            file_options={"content-type": "video/mp4", "x-upsert": "true"}
        )
    
    public_url = supabase.storage.from_("videos").get_public_url(file_name)

    # 5. Generate Questions (AI)
    status_text.text("🧠 Generating Coursework...")
    progress_bar.progress(70)
    
    prompt = f"""
    Analyze the following transcript. Generate 5 Multiple Choice Questions (Stages 0-4).
    CRITICAL RULES: Difficulty HARD. JSON Output.
    Format: {{ "questions": [ {{ "stage": 0, "q": "...", "correct": "...", "wrong": [...] }} ] }}
    
    TRANSCRIPT: {transcript_text[:15000]}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
            {"role": "user", "content": prompt}
        ],
        response_format={ "type": "json_object" }
    )
    questions_json = json.loads(response.choices[0].message.content)
    
    # 6. Save to Database
    status_text.text("💾 Saving to Database...")
    progress_bar.progress(90)
    
    full_category = f"{category} > {sub_category}" if sub_category else category
    
    video_data = {
        "video_url": public_url,
        "title": title,
        "transcript_text": transcript_text,
        "category": full_category,
        "duration_seconds": int(duration)
    }
    
    data = supabase.table("content_library").insert(video_data).execute()
    new_video_id = data.data[0]['id']
    
    q_inserts = []
    for item in questions_json['questions']:
        q_inserts.append({
            "video_id": new_video_id,
            "difficulty_phase": item['stage'],
            "question_text": item['q'],
            "correct_answer": item['correct'],
            "wrong_options": item['wrong']
        })
    supabase.table("questions").insert(q_inserts).execute()
    
    # Cleanup
    os.remove(temp_video_path)
    os.remove(audio_path)
    
    progress_bar.progress(100)
    status_text.text("✅ DONE!")
    st.balloons()

# --- UI LAYOUT ---
st.set_page_config(page_title="Fluency Admin", page_icon="🧠")

st.title("🧠 Fluency Curator")
st.markdown("Upload content to generate spaced-repetition courses.")

with st.form("upload_form"):
    # File Input
    uploaded_file = st.file_uploader("Choose a Video (MP4)", type=["mp4", "mov"])
    
    col1, col2 = st.columns(2)
    with col1:
        title = st.text_input("Video Title", placeholder="e.g. Latent Demand Explained")
    with col2:
        # Pre-defined categories for consistency
        category = st.selectbox("Category", ["Product Management", "Philosophy", "History", "Science", "Language", "Other"])
        sub_category = st.text_input("Sub-Category (Optional)", placeholder="e.g. User Research")

    submitted = st.form_submit_button("🚀 Process & Publish")

# Placeholders for progress updates
status_text = st.empty()
progress_bar = st.progress(0)

if submitted and uploaded_file and title:
    process_video(uploaded_file, title, category, sub_category)
elif submitted:
    st.warning("Please upload a file and give it a title.")

# Preview Section
if uploaded_file:
    st.video(uploaded_file)