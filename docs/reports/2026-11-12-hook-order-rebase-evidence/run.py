import json, os, pathlib, shlex, subprocess, sys, time
scratch = pathlib.Path('/private/tmp/sf-hor-b1')
env = os.environ.copy()
env.update(GIT_DIR=str(scratch/'git'), TMPDIR=str(scratch), UV_CACHE_DIR=str(scratch/'uv-cache'), UV_PYTHON_INSTALL_DIR=str(scratch/'uv-python'), HYPOTHESIS_STORAGE_DIRECTORY=str(scratch/'hypothesis'))
env['PYTHONPATH'] = str(scratch) + os.pathsep + str(pathlib.Path.cwd())
env.update(CLANG_MODULE_CACHE_PATH=str(scratch/'clang-cache'), SWIFT_MODULECACHE_PATH=str(scratch/'swift-cache'), SUBFLEET_SWIFT_NESTED_SANDBOX='off')
command = sys.argv[1:]
if command[0] != 'git':
    env.pop('GIT_DIR', None)
name = str(time.time_ns())
start = time.monotonic()
log = scratch / (name+'.log')
with log.open('w') as stream:
    result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT, text=True)
output = log.read_text()
record = dict(command=shlex.join(command), exit=result.returncode, seconds=round(time.monotonic()-start,2), log=str(log))
with (scratch/'commands.jsonl').open('a') as stream:
    stream.write(json.dumps(record)+'\n')
print(json.dumps(record), flush=True)
print(output[-24000:], end='', flush=True)
sys.exit(result.returncode)
