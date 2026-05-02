# YouTube Knowledge Base

An intelligent video analysis pipeline that transforms YouTube videos (or local files) into comprehensive study guides using RocketRide and Gemini.

## Features
- **Video Analysis**: Automatically transcribes audio, extracts slide frames, and runs OCR.
- **AI-Powered Insights**: Generates chapters, summaries, key takeaways, and glossary terms.
- **Interactive Learning**: Creates flashcards and quizzes directly from the video content.
- **Metadata Management**: Captures video titles, thumbnails, and duration automatically.

## Tech Stack
- **Backend**: FastAPI, RocketRide (AI Pipeline Engine)
- **AI Models**: Gemini 1.5 Flash / 3.1 Flash Lite
- **Transcription**: Whisper (via RocketRide)
- **Computer Vision**: OpenCV, EasyOCR (via RocketRide)
- **Database**: Qdrant (Vector storage for RAG)

## Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/yourusername/youtube-knowledge-base.git
   cd youtube-knowledge-base
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   Create a `.env` file based on `.env.example`:
   ```bash
   ROCKETRIDE_URI=http://localhost:5565
   ROCKETRIDE_APIKEY=your_rocketride_api_key
   ROCKETRIDE_GEMINI_APIKEY=your_google_ai_key
   ```

4. **Run the application**:
   ```bash
   uvicorn app:app --reload
   ```

## Usage
- Open `http://localhost:8000` in your browser.
- Paste a YouTube URL or a local file path.
- Wait for the analysis to complete.
- Browse the interactive study guide, quiz, and flashcards.

## Pipeline Structure
The core logic resides in `pipelines/youtube-url-to-study-guide.pipe`. This definition handles:
1. `webhook` -> Binary video input
2. `parse` -> Media splitting
3. `audio_transcribe` -> Text extraction
4. `frame_grabber` + `ocr` -> Visual text extraction
5. `llm_gemini` -> Final JSON generation
