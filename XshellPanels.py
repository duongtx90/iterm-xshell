#!/usr/bin/env python3
"""Entry point cho iTerm2: panel kênh SSH + panel duyệt file SFTP (giống Xshell + Xftp).

Thường được khởi chạy bởi AutoLaunch/XshellPanels.scpt (xem install.sh), có thể chạy tay:
    ~/Library/Application\\ Support/iterm-xshell/venv/bin/python XshellPanels.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xshell_panels.daemon import run  # noqa: E402

if __name__ == "__main__":
    run()
