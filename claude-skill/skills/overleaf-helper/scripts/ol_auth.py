"""Overleaf session cookie: store it, check it, delete it.

Overleaf is used with the organisation login, so there is no password to script. The tool uses the
session cookie of the logged-in browser instead: the value of overleaf_session2, copied from Chrome.
It is kept in the system credential store (Windows Credential Manager) and never printed or written
to a file.
"""
import getpass
import html
import re

import keyring
import keyring.errors
import requests

BASE_URL = "https://www.overleaf.com"
COOKIE = "overleaf_session2"
SERVICE = "overleaf-helper"
USER_AGENT = "overleaf-helper/0.1"
HOW_TO_COPY = (f"Copy it in Chrome on overleaf.com: F12, Application, Cookies, {BASE_URL}, {COOKIE}, "
               "Value. Then run: overleaf.py login")


class AuthError(Exception):
    """No cookie stored, or Overleaf does not accept it."""


def clean(raw):
    """The cookie value in what was pasted, or None when it does not look like one."""
    value = raw.strip().strip('"')
    if value.startswith(COOKIE + "="):
        value = value[len(COOKIE) + 1:]
    if not value or any(char.isspace() or char == ";" for char in value):
        return None
    return value


def read_clipboard():
    """Text on the clipboard. The clipboard is cleared afterwards, since it may hold the cookie."""
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        text = root.clipboard_get()
    except tkinter.TclError:  # empty, or no text on it
        text = ""
    root.clipboard_clear()
    root.update()
    root.destroy()
    return text


def session(cookie):
    """A requests session that sends the Overleaf cookie."""
    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    http.cookies.set(COOKIE, cookie, domain=".overleaf.com")
    return http


def check(cookie):
    """Email of the account the cookie belongs to. Without a valid session, Overleaf answers the
    project list with a redirect to the login page."""
    response = session(cookie).get(f"{BASE_URL}/project", allow_redirects=False, timeout=30)
    if response.is_redirect or response.status_code in (401, 403):
        raise AuthError(f"Overleaf does not accept the cookie: it has expired or is not the value of "
                        f"{COOKIE}. {HOW_TO_COPY}")
    response.raise_for_status()
    match = re.search(r'<meta[^>]*name="ol-usersEmail"[^>]*content="([^"]*)"', response.text)
    return html.unescape(match.group(1)) if match else "(email not found on the page)"


def login(from_clipboard=False):
    """Check the cookie from a hidden prompt or the clipboard and store it. Returns the email."""
    raw = read_clipboard() if from_clipboard else getpass.getpass(f"Value of {COOKIE} (input hidden): ")
    cookie = clean(raw)
    if not cookie:
        where = "on the clipboard" if from_clipboard else "in the input"
        raise AuthError(f"No cookie found {where}. {HOW_TO_COPY}")
    email = check(cookie)
    keyring.set_password(SERVICE, COOKIE, cookie)
    return email


def stored_cookie():
    cookie = keyring.get_password(SERVICE, COOKIE)
    if not cookie:
        raise AuthError(f"Not logged in. {HOW_TO_COPY}")
    return cookie


def whoami():
    return check(stored_cookie())


def logout():
    """Delete the stored cookie. The session on Overleaf stays valid until it expires."""
    try:
        keyring.delete_password(SERVICE, COOKIE)
    except keyring.errors.PasswordDeleteError:
        pass
