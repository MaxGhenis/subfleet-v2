"""Elapsed-time generators ported from the reviewer's parser_probes.py.

Apple adv_cmds ps/print.c get_etime uses strict > transitions:
https://github.com/apple-oss-distributions/adv_cmds/blob/main/ps/print.c
procps-ng src/ps/output.c pr_etime uses >= transitions:
https://github.com/procps-ng/procps/blob/master/src/ps/output.c
These source formatter rules are independent of the workflow's parser.
"""


def darwin(age):
    if age > 100 * 86400:
        return f"{age // 86400}-{age // 3600 % 24:02}:{age // 60 % 60:02}:{age % 60:02}"
    if age > 86400:
        return f"{age // 86400:02}-{age // 3600 % 24:02}:{age // 60 % 60:02}:{age % 60:02}"
    if age > 3600:
        return f"{age // 3600:02}:{age // 60 % 60:02}:{age % 60:02}"
    return f"{age // 60:02}:{age % 60:02}"


def procps(age):
    prefix = f"{age // 86400}-" if age >= 86400 else ""
    if age >= 3600:
        prefix += f"{age // 3600 % 24:02}:"
    return prefix + f"{age // 60 % 60:02}:{age % 60:02}"
