import time
import urllib.request


def fetch(url):
    for attempt in range(5):
        try:
            return urllib.request.urlopen(url).read()
        except Exception:
            time.sleep(2**attempt)
    return None
