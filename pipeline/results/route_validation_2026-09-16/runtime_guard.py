"""Startup readiness and bounded calls; never reconnect a live UE session."""
import signal,time

def editor_ready(log_text):
    # TCP is bound before editor startup and PIE world duplication finish.
    return ('Start listening on port 9208' in log_text
            and 'PIE: Play in editor total start time' in log_text)

class RpcTimeout(TimeoutError):
    pass

def bounded_call(label,fn,*args,timeout_s=180,**kwargs):
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError('Nested RPC deadlines are not supported')
    def expired(*_):
        raise RpcTimeout(f'{label} did not reply within {timeout_s:g}s; editor restart required')
    old=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,timeout_s)
    try:
        return fn(*args,**kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,old)
