import sys
if len(sys.argv) > 1 and sys.argv[1] == 'diagnostic':
    # No configuration, credentials, production Store, HTTP, or worker imported.
    import json, os, socket, tempfile
    from pathlib import Path
    def denied(*args, **kwargs): raise RuntimeError('diagnostic network prohibited')
    socket.socket = denied
    from .core import run_lock
    outcome = sys.argv[2] if len(sys.argv)>2 else 'complete'
    with tempfile.TemporaryDirectory(prefix='paper-diagnostic-') as temp:
        with run_lock(Path(temp)):
            print(json.dumps({'entry':'Windows -> WSL -> Python','executable':sys.executable,'cwd':os.getcwd(),'network':False,'model':False,'translation':False,'outcome':outcome}),flush=True)
            if outcome == 'abrupt': os._exit(137)
            code={'complete':0,'failed':1,'paused':130}[outcome]
    raise SystemExit(code)
from .cli import main
raise SystemExit(main())
