#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise native locking and execution with a Nix-provided JVM module."""
from pathlib import Path
import json, os, shutil, subprocess, tempfile

root = Path(__file__).resolve().parent.parent
system = subprocess.check_output(
    ['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'], text=True).strip()
snapshot = json.loads(subprocess.check_output(
    ['nix', 'flake', 'metadata', '--json', '--no-write-lock-file', 'path:' + str(root)], text=True))['path']
with tempfile.TemporaryDirectory(prefix="purifier-native-composition-") as temporary:
    temporary = Path(temporary)
    subprocess.run(['nix-store', '--add-root', str(temporary / 'source'), '--realise', snapshot], check=True)
    expression = """let
      f = builtins.getFlake ("path:" + builtins.getEnv "COMPOSITION_FLAKE");
      p = f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
    in p.runCommand "composition-downstream-tools" {} ''
      mkdir -p "$out/bin"
      ln -s ${p.maven}/bin/mvn "$out/bin/mvn"
      ln -s ${p.jdk21_headless.home} "$out/jdk"
    '' """
    tools = Path(subprocess.check_output(['nix', 'build', '--impure', '--print-out-paths',
        '--out-link', str(temporary / 'tools'), '--expr', expression], text=True,
        env=dict(os.environ, COMPOSITION_FLAKE=str(root))).strip())
    package = subprocess.check_output(
        ['nix', 'build', str(root) + '#default', '--print-out-paths',
         '--out-link', str(temporary / 'cli')], text=True).strip().splitlines()[-1]
    cli = Path(package) / 'bin/mill-purifier'
    for case, provided_module, provided_version, attribute in (
        ('computed', 'upickle', '99.0.0', 'provider'),
        ('transitive', 'upickle-core', '4.1.0', 'coreProvider'),
    ):
        workspace = temporary / case
        shutil.copytree(root / 'tests/fixtures/simple', workspace)
        (workspace / 'mill-purifier.lock.json').unlink(missing_ok=True)
        if case == 'computed':
            definition = workspace / 'build.mill'
            contents = definition.read_text()
            assert 'mvn"com.lihaoyi::upickle:4.1.0"' in contents
            contents = contents.replace('def mvnDeps = Seq(mvn"com.lihaoyi::upickle:4.1.0")',
                'def selectedVersion = "99.0.0"\n  def mvnDeps = Seq(mvn"com.lihaoyi::upickle:${selectedVersion}")')
            definition.write_text(contents)
        reference='path:'+snapshot+'#checks.'+system+'.composition.'+attribute
        env=dict(os.environ,MILL_PURIFIER_MAVEN_CENTRAL_MIRROR='https://repo.maven.apache.org/maven2')
        def run(*args):
            subprocess.run([str(x) for x in args],cwd=workspace,env=env,check=True)
        run(cli,'lock','--input',reference)
        locked=json.loads((workspace/'mill-purifier.lock.json').read_text())
        assert len(locked['projectInputs'])==1
        supplied = f'/com/lihaoyi/{provided_module}_3/{provided_version}/'
        assert not any(supplied in entry['path'] for entry in locked['artifacts'])
        bootstrap = f'/com/lihaoyi/{provided_module}_3/4.4.3/'
        assert any(bootstrap in entry['path'] for entry in locked['artifacts'])
        run(cli,'prefetch')
        run(cli,'mill','core.test')
        expression = """let
          flake = builtins.getFlake (builtins.getEnv "MILL_COMPOSITION_SOURCE");
          project = builtins.getEnv "MILL_COMPOSITION_PROJECT";
          build = import (flake.outPath + "/nix/lib/cli.nix") {
            nixpkgs = flake.inputs.nixpkgs;
            purifierSource = flake.inputs.purifier;
            src = project;
            lockFile = project + "/mill-purifier.lock.json";
          };
        in [ build.tests build.release ]"""
        built = json.loads(subprocess.check_output(['nix', 'build', '--impure', '--json', '--expr', expression,
                        '--out-link', str(temporary / (case+'-pure-tests'))], text=True,
                       env=dict(env, MILL_COMPOSITION_SOURCE='path:'+snapshot,
                                MILL_COMPOSITION_PROJECT=str(workspace))))
        repository = Path(next(build['outputs']['out'] for build in built
                               if build['outputs']['out'].endswith('-mill-release')))
        pom = next((repository / 'example/core_3').glob('*/*.pom'))
        group, artifact = 'example', 'core_3'
        names = ['upickle.default$'] + (['upickle.core.Visitor'] if case == 'transitive' else [])
        invoke = 'Class.forName("example.Encode").getMethod("main", String[].class).invoke(null, (Object)new String[0]);'
        downstream = temporary / (case+'-downstream')
        source = downstream / 'src/main/java/Downstream.java'
        source.parent.mkdir(parents=True)
        checks = '\n'.join('''String origin%d = Class.forName(%s).getProtectionDomain().getCodeSource().getLocation().toString();
          if (!origin%d.contains("-nix-")) throw new AssertionError(origin%d);''' % (index, json.dumps(name), index, index)
          for index, name in enumerate(names))
        source.write_text('public final class Downstream { public static void main(String[] args) throws Exception {'
                          + invoke + checks + 'System.out.println("ORDINARY MAVEN PASSED"); } }')
        (downstream / 'pom.xml').write_text(f'''<project><modelVersion>4.0.0</modelVersion>
          <groupId>example</groupId><artifactId>downstream</artifactId><version>1</version>
          <properties><maven.compiler.release>21</maven.compiler.release></properties>
          <repositories><repository><id>projected</id><url>{repository.as_uri()}</url></repository></repositories>
          <dependencies><dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>
          <version>{pom.parent.name}</version></dependency></dependencies></project>''')
        plain = dict(os.environ, JAVA_HOME=str(tools / 'jdk'))
        subprocess.run([str(tools / 'bin/mvn'), '-B', '-Dmaven.repo.local='+str(temporary / 'ordinary-m2'),
                        'package', 'dependency:build-classpath', '-Dmdep.outputFile=classpath'],
                       cwd=downstream, env=plain, check=True)
        subprocess.run([str(tools / 'jdk/bin/java'), '-cp', str(downstream / 'target/classes')+os.pathsep
                        +(downstream / 'classpath').read_text().strip(), 'Downstream'],
                       cwd=downstream, env=plain, check=True)
        print('MILL NATIVE AND PURE '+case.upper()+' ROUNDTRIP PASSED', flush=True)

    # tests/default.nix has the polyglot checks only on the systems whose
    # polyglot lock holds JVM archives, the Linux ones.
    if not json.loads(subprocess.check_output(['nix', 'eval', '--json', 'path:' + snapshot + '#checks.' + system,
            '--apply', 'checks: checks ? polyglot-hello-package'], text=True)):
        print('skipping the polyglot round trip: there is no polyglot-hello-package check on ' + system, flush=True)
        raise SystemExit(0)
    reference = 'path:' + snapshot + '#checks.' + system + '.polyglot-hello-package.projections.maven'
    repository = Path(subprocess.check_output(['nix', 'build', reference, '--print-out-paths',
        '--out-link', str(temporary / 'polyglot-repository')], text=True).strip().splitlines()[-1])
    pom = next((repository / 'example/hello').glob('*/*.pom'))
    downstream = temporary / 'polyglot-downstream'
    source = downstream / 'src/main/java/Downstream.java'
    source.parent.mkdir(parents=True)
    source.write_text('public final class Downstream {\n      public static void main(String[] args) throws Exception {\n        Class.forName("example.HelloKt").getMethod("main", String[].class)\n          .invoke(null, (Object)new String[0]);\n        for (String name : new String[]{"example.Greeter", "kotlin.Unit"}) {\n          String origin = Class.forName(name).getProtectionDomain().getCodeSource().getLocation().toString();\n          if (!origin.contains("-nix-")) throw new AssertionError(origin);\n        }\n      }\n    }')
    (downstream / 'pom.xml').write_text(f"""<project><modelVersion>4.0.0</modelVersion>
      <groupId>example</groupId><artifactId>downstream</artifactId><version>1</version>
      <properties><maven.compiler.release>21</maven.compiler.release></properties>
      <repositories><repository><id>projected</id><url>{repository.as_uri()}</url></repository></repositories>
      <dependencies><dependency><groupId>example</groupId><artifactId>hello</artifactId>
      <version>{pom.parent.name}</version></dependency></dependencies></project>""")
    plain = dict(os.environ, JAVA_HOME=str(tools / 'jdk'))
    subprocess.run([str(tools / 'bin/mvn'), '-B', '-Dmaven.repo.local='+str(temporary / 'ordinary-m2'),
        'package', 'dependency:build-classpath', '-Dmdep.outputFile=classpath'],
        cwd=downstream, env=plain, check=True)
    output = subprocess.check_output([str(tools / 'jdk/bin/java'), '-cp',
        str(downstream / 'target/classes')+os.pathsep+(downstream / 'classpath').read_text().strip(),
        'Downstream'], cwd=downstream, env=plain, text=True)
    assert 'hello java, kotlin' in output, output
    print('MILL POLYGLOT ORDINARY MAVEN ROUNDTRIP PASSED', flush=True)
