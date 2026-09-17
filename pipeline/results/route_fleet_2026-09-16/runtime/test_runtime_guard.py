import signal,time
from runtime_guard import editor_ready,bounded_call,RpcTimeout
assert not editor_ready('Start listening on port 9208')
assert not editor_ready('PIE: Created PIE world by copying editor world')
assert not editor_ready('PIE: Play in editor total start time 7s')
assert editor_ready('Start listening on port 9208\nPIE: Play in editor total start time 7s')
handler=signal.getsignal(signal.SIGALRM)
assert bounded_call('good',lambda:123,timeout_s=.1)==123
try:bounded_call('injected blocked RPC',time.sleep,.2,timeout_s=.02)
except RpcTimeout as e:assert 'injected blocked RPC' in str(e)
else:raise AssertionError('Timed-out RPC was not rejected')
assert signal.getitimer(signal.ITIMER_REAL)[0]==0
assert signal.getsignal(signal.SIGALRM)==handler
assert bounded_call('fresh operation',lambda:456,timeout_s=.1)==456
print('Passed: listener-only readiness rejected; PIE completion required; blocked RPC timed out; signal deadline cleaned up')
