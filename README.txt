Shorts Archive backend fixed build

Replace the backend files in your Render-connected GitHub repository with these files.
The important fix is in app.py:
- channel URLs are normalized to the /shorts tab
- discovery captures real yt-dlp stderr/exit codes
- yt-dlp gets one bgutil provider argument
- Android VR is tried first because it does not require a GVS PO token
