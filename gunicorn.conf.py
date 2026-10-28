# the default access log, plus how long each request took (%(L)s, seconds), plus the real client: %(h)s is Fly's own
# proxy address, so the client comes from Cloudflare's CF-Connecting-IP (or Fly-Client-IP when it's bypassed)
access_log_format = ('%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s '
                     'cf=%({cf-connecting-ip}i)s fly=%({fly-client-ip}i)s')
