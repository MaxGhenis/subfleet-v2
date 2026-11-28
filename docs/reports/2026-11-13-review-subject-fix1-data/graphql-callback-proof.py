"""Call the synthetic GraphQL predicate as data, with no command execution."""

import json
from sf_fix1_graphql import ARGV, BODY, NotSyntheticRead, synthetic_reply

query = 'query { p0: repository(owner:"o",name:"r") { pullRequest(number:1) { state } } }'
kwargs = {'input': json.dumps({'query': query})}
reply = synthetic_reply(ARGV, kwargs, BODY)
assert reply.returncode == 0 and json.loads(reply.stdout) == BODY
checks = 1
cases = (
    (['gh', 'api', '-X', 'POST', 'repos/nonexistent-sf-fix1/absent/issues'], kwargs, BODY),
    (ARGV, {'input': json.dumps({'query': query.replace('query', 'mutation', 1)})}, BODY),
    (ARGV, {'input': json.dumps({'query': query.replace('number:1', 'number:2')})}, BODY),
    (ARGV, {'input': json.dumps({'query': query, 'extra': 'synthetic'})}, BODY),
    (ARGV, kwargs, {'data': {}}),
)
for argv, arguments, body in cases:
    try:
        synthetic_reply(argv, arguments, body)
    except NotSyntheticRead:
        checks += 1
    else:
        raise AssertionError('synthetic callback admitted unreviewed inert input')
print(json.dumps({'checks': checks, 'external_commands_executed': 0}))
