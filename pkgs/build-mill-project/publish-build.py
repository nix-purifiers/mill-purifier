# SPDX-License-Identifier: MIT
"""Describe immutable projected files to Mill's native publication tasks."""
import json
from pathlib import Path
import sys

repository, catalog, output = map(Path, sys.argv[1:4])
version = sys.argv[4]
lines = [f'//| mill-version: {version}', '//| mill-jvm-version: system', 'package build',
         'import mill.*, mill.javalib.*, mill.javalib.publish.*']
description = json.loads(catalog.read_text())
for index, module in enumerate(description.get('publications', description['modules'])):
    group, name, revision = (module[key] for key in ('group', 'name', 'version'))
    directory = repository / group.replace('.', '/') / name / revision
    payload = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix not in ('.md5', '.sha1', '.sha256', '.sha512', '.asc'):
            payload.append('        PathRef(os.Path(' + json.dumps(str(path)) + ')) -> ' + json.dumps(path.name))
    lines += ['', f'object m{index} extends Module {{', '  def publishArtifacts = Task {',
              '    PublishModule.PublishData(',
              '      Artifact(' + ', '.join(json.dumps(value) for value in (group, name, revision)) + '),',
              '      Seq(', ',\n'.join(payload), '      )', '    )', '  }', '}']
output.write_text('\n'.join(lines) + '\n')
