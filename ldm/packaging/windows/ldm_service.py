# ldm-service.exe: the download service without a console window (started at login and by the window and the
# browser bridge); it logs to %LOCALAPPDATA%\LDM\Logs\daemon.log.
import sys

from ldm.cli import main

sys.exit(main([*sys.argv[1:], "daemon"]))  # options such as --db come before "daemon"
