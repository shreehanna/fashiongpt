# Gunicorn reads this file by itself (it's in the folder the app starts from), so Render's start
# command doesn't need changing.
#
# threads: one worker, 8 requests at a time. Without this, gunicorn handles ONE request at a time:
#   while Ella writes a look (15-25 s), every product photo, shop link and other visitor waits.
# timeout: a look that has to be restyled can take a minute; don't kill it at gunicorn's default 30 s.
workers = 1
threads = 8
timeout = 120
graceful_timeout = 30
