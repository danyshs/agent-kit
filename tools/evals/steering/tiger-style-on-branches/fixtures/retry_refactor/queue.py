import time


def drain(jobs, handler):
    done = []
    for job in jobs:
        tries = 0
        while True:
            try:
                done.append(handler(job))
                break
            except Exception:
                tries += 1
                if tries > 4:
                    break
                time.sleep(2**tries)
    return done
