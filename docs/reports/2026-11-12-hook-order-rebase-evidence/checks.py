from pathlib import Path
import subprocess, sys
scratch=Path('/private/tmp/sf-hor-b1')
if sys.argv[1]=='targeted':
    found=[line for line in (scratch/'targeted-filenames.log').read_text().splitlines()
           if Path(line).name.startswith('test_') and line.endswith('.py')]
    priority=['tests/unit/test_hooks.py','tests/unit/test_doctor.py','tests/unit/test_hooks_native_guard.py','tests/unit/test_live_store_guard_round4.py','tests/unit/test_compat.py','tests/unit/test_daemon_verbs.py']
    files=priority+[name for name in found if name not in priority]
    assert len(files)==13,len(files)
    args=['-n','4',*files,'--basetemp='+str(scratch/'targeted')]
else:
    args=['tests/fake/test_state_contract.py','tests/unit/test_test_module_names.py','tests/unit/test_invariants_index.py','tests/unit/test_handler_refusals.py','--basetemp='+str(scratch/'contracts')]
result=subprocess.run(['python3',str(scratch/'run.py'),'.venv/bin/python','-m','pytest','-p','sf_job_paths',*args,'-q'])
sys.exit(result.returncode)
