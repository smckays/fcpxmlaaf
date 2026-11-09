"""FCPXML to AAF — app entry point (run by the bundled Python from the .app launcher)."""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.dirname(HERE)
for p in (os.path.join(RES, "engine"), os.path.join(RES, "lib"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import server  # noqa: E402

TITLE = "FCPXML to AAF"


def brand_process():
    """The app runs inside the bundled Python; give the Dock our name and icon instead of Python's."""
    try:
        from Foundation import NSBundle
        from AppKit import NSApplication, NSImage
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = TITLE
        icon = os.path.join(RES, "AppIcon.icns")
        if os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception as e:
        print("branding skipped:", e)


def native_window(url) -> bool:
    try:
        import webview
    except Exception as e:
        print("native window unavailable:", e)
        return False
    brand_process()
    try:
        webview.create_window(TITLE, url, width=1240, height=860, min_size=(960, 640), background_color="#121614")
        webview.start()
        return True
    except Exception as e:
        print("native window failed:", e)
        return False


def browser_window(url):
    chrome = "/Applications/Google Chrome.app"
    if os.path.exists(chrome):  # app-style window without browser chrome
        subprocess.run(["open", "-na", chrome, "--args", "--app=" + url, "--window-size=1240,860"])
    else:
        subprocess.run(["open", url])
    # stay alive while the page keeps checking in
    while time.time() - server.App.last_beat < 120:
        time.sleep(5)


def main():
    srv, url = server.start()
    print("serving", url.split("#")[0], flush=True)
    opened = native_window(url)
    if not opened:
        server.App.last_beat = time.time()
        browser_window(url)
    srv.shutdown()


if __name__ == "__main__":
    main()
