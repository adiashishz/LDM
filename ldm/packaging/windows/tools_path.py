# PyInstaller runtime hook: every program of LDM's Windows bundle runs this first. The bundle's tools folder
# (ffmpeg, ffprobe, node) goes first on PATH, for yt-dlp and LDM, and for the processes they start.
import os
import sys

_tools = os.path.join(sys._MEIPASS, "tools")
os.environ["PATH"] = _tools + os.pathsep + os.environ.get("PATH", "")
