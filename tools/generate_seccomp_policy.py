#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This script will take any number of trace files generated by strace(1)
# and output a system call filtering policy suitable for use with Minijail.

"""Helper tool to generate a minijail seccomp filter from strace output."""

from __future__ import print_function

import argparse
import collections
import re
import sys


NOTICE = """# Copyright (C) 2018 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""

ALLOW = '1'

# This ignores any leading PID tag and trailing <unfinished ...>, and extracts
# the syscall name and the argument list.
LINE_RE = re.compile(r'^\s*(?:\[[^]]*\]|\d+)?\s*([a-zA-Z0-9_]+)\(([^)<]*)')

SOCKETCALLS = {
    'accept', 'bind', 'connect', 'getpeername', 'getsockname', 'getsockopt',
    'listen', 'recv', 'recvfrom', 'recvmsg', 'send', 'sendmsg', 'sendto',
    'setsockopt', 'shutdown', 'socket', 'socketpair',
}

ArgInspectionEntry = collections.namedtuple('ArgInspectionEntry',
                                            ('arg_index', 'value_set'))


def parse_args(argv):
    """Returns the parsed CLI arguments for this tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frequency', nargs='?', type=argparse.FileType('w'),
                        help='frequency file')
    parser.add_argument('--policy', nargs='?', type=argparse.FileType('w'),
                        default=sys.stdout, help='policy file')
    parser.add_argument('traces', nargs='+', help='The strace logs.')
    return parser.parse_args(argv)


def get_seccomp_bpf_filter(syscall, entry):
    """Return a minijail seccomp-bpf filter expression for the syscall."""
    arg_index = entry.arg_index
    arg_values = entry.value_set
    atoms = []
    if syscall in ('mmap', 'mmap2', 'mprotect') and arg_index == 2:
        # See if there is at least one instance of any of these syscalls trying
        # to map memory with both PROT_EXEC and PROT_WRITE. If there isn't, we
        # can craft a concise expression to forbid this.
        write_and_exec = set(('PROT_EXEC', 'PROT_WRITE'))
        for arg_value in arg_values:
            if write_and_exec.issubset(set(p.strip() for p in
                                           arg_value.split('|'))):
                break
        else:
            atoms.extend(['arg2 in ~PROT_EXEC', 'arg2 in ~PROT_WRITE'])
            arg_values = set()
    atoms.extend('arg%d == %s' % (arg_index, arg_value)
                 for arg_value in arg_values)
    return ' || '.join(atoms)


def parse_trace_file(trace_filename, syscalls, arg_inspection):
    """Parses one file produced by strace."""
    uses_socketcall = ('i386' in trace_filename or
                       ('x86' in trace_filename and
                        '64' not in trace_filename))

    with open(trace_filename) as trace_file:
        for line in trace_file:
            matches = LINE_RE.match(line)
            if not matches:
                continue

            syscall, args = matches.groups()
            if uses_socketcall and syscall in SOCKETCALLS:
                syscall = 'socketcall'

            syscalls[syscall] += 1

            args = [arg.strip() for arg in args.split(',')]

            if syscall in arg_inspection:
                arg_value = args[arg_inspection[syscall].arg_index]
                arg_inspection[syscall].value_set.add(arg_value)


def main(argv):
    """Main entrypoint."""
    opts = parse_args(argv)

    syscalls = collections.defaultdict(int)

    arg_inspection = {
        'socket': ArgInspectionEntry(0, set([])),   # int domain
        'ioctl': ArgInspectionEntry(1, set([])),    # int request
        'prctl': ArgInspectionEntry(0, set([])),    # int option
        'mmap': ArgInspectionEntry(2, set([])),     # int prot
        'mmap2': ArgInspectionEntry(2, set([])),    # int prot
        'mprotect': ArgInspectionEntry(2, set([])), # int prot
    }

    for trace_filename in opts.traces:
        parse_trace_file(trace_filename, syscalls, arg_inspection)

    # Add the basic set if they are not yet present.
    basic_set = [
        'restart_syscall', 'exit', 'exit_group', 'rt_sigreturn',
    ]
    for basic_syscall in basic_set:
        if basic_syscall not in syscalls:
            syscalls[basic_syscall] = 1

    # If a frequency file isn't used then sort the syscalls based on frequency
    # to make the common case fast (by checking frequent calls earlier).
    # Otherwise, sort alphabetically to make it easier for humans to see which
    # calls are in use (and if necessary manually add a new syscall to the
    # list).
    if opts.frequency is None:
        sorted_syscalls = list(
            x[0] for x in sorted(syscalls.items(), key=lambda pair: pair[1],
                                 reverse=True)
        )
    else:
        sorted_syscalls = list(
            x[0] for x in sorted(syscalls.items(), key=lambda pair: pair[0])
        )

    print(NOTICE, file=opts.policy)
    if opts.frequency is not None:
        print(NOTICE, file=opts.frequency)

    for syscall in sorted_syscalls:
        if syscall in arg_inspection:
            arg_filter = get_seccomp_bpf_filter(syscall, arg_inspection[syscall])
        else:
            arg_filter = ALLOW
        print('%s: %s' % (syscall, arg_filter), file=opts.policy)
        if opts.frequency is not None:
            print('%s: %s' % (syscall, syscalls[syscall]),
                  file=opts.frequency)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
