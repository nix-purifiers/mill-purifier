# SPDX-License-Identifier: MIT
"""Scope native Mill publication closures and merge their versioned views."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import jvm_repository as jvm

mode = sys.argv[2]
destination = Path(sys.argv[3])
if mode == 'native':
    for source in map(Path, sys.argv[4:]):
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_file() and 'nix-support' not in relative.parts:
                jvm.write(destination/relative, path.read_bytes())
elif mode == 'scope':
    source, description = map(Path, sys.argv[4:])
    root = json.loads(description.read_text())
    wanted = root['group'], root['id'], root['version']
    models = {}
    for path in source.rglob('*.pom'):
        model, properties, coordinate = jvm.pom_model(path)
        models[coordinate] = path, model, properties
    pending, visited = [wanted], set()
    while pending:
        coordinate = pending.pop()
        if coordinate in visited:
            continue
        visited.add(coordinate)
        path, model, properties = models[coordinate]
        name, version = coordinate[1:]
        for artifact in sorted(path.parent.iterdir()):
            if artifact.is_file():
                jvm.write(destination/artifact.relative_to(source), artifact.read_bytes())
        for key, requested in jvm.descriptor_dependencies(path, model, properties):
            for candidate in models:
                if candidate[:2] == key and jvm.matches_selection(requested, candidate[2]):
                    pending.append(candidate)
elif mode == 'merge':
    metadata = Path(sys.argv[4])
    modules = {}
    for index in range(5, len(sys.argv), 2):
        repository, catalog = map(Path, sys.argv[index:index+2])
        jvm.merge(repository, destination)
        for module in json.loads(catalog.read_text())['modules']:
            key = module['group'], module['name'], module['version']
            previous = modules.setdefault(key, module)
            if previous != module:
                raise ValueError('conflicting immutable Mill component: '+str(key))
    # Model-only parents and BOMs are publications too, but do not become
    # dependency selectors merely because a runtime descriptor references them.
    publications = []
    for path in sorted(destination.rglob('*.pom')):
        version, name = path.parent.name, path.parent.parent.name
        if path.name == name+'-'+version+'.pom':
            group = '.'.join(path.parent.parent.parent.relative_to(destination).parts)
            jvm.validate_coordinates(group, name, version)
            publications.append({'group': group, 'name': name, 'version': version})
    jvm.write(metadata/'catalog.json', (json.dumps({'version': 1, 'producer': os.environ['producerIdentity'],
        'modules': [modules[key] for key in sorted(modules)],
        'publications': publications}, sort_keys=True)+'\n').encode())
else:
    raise ValueError('unknown native publication operation: '+mode)
