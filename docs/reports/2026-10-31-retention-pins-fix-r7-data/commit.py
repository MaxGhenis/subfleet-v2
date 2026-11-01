"""Commit an explicitly staged step without refreshing unrelated checkout files."""
import subprocess
import sys
prefix = ['git', '--git-dir=.retention-r7-git', '-c', 'commit.gpgsign=false']
def git(*args, **kwargs):
    return subprocess.check_output(prefix + list(args), text=True, **kwargs).strip()
parent = git('rev-parse', 'HEAD')
tree = git('write-tree')
message = sys.argv[1] + '\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n'
commit = git('commit-tree', tree, '-p', parent, input=message)
git('update-ref', 'refs/heads/fix/retention-dependency-pins-217', commit, parent)
print(commit)
