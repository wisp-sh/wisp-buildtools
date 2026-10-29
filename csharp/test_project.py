"""Input policy tests; no compiler bootstrap or native SDK is needed."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from project import Inputs, package_closure, packages

class ProjectTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '.toolchains' / 'input-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, text):
        path = self.root / path; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); return path

    def project(self, path, items='', properties=''):
        return self.write(path, '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>' + properties + '</PropertyGroup><ItemGroup>' + items + '</ItemGroup></Project>')

    def test_project_graph_and_source_selection(self):
        self.project('Shared/Shared.csproj'); self.write('Shared/Models.cs', 'class Model {}')
        main = self.project('Api/Api.csproj', '<ProjectReference Include="../Shared/Shared.csproj"/><Compile Remove="Bad.cs"/>', '<ImplicitUsings>enable</ImplicitUsings>')
        self.write('Api/Api.cs', 'class Api {}'); self.write('Api/Bad.cs', 'bad'); self.write('Api/obj/Generated.cs','bad')
        inputs = Inputs([main], self.root)
        self.assertEqual([p.name for p in inputs.order], ['Shared', 'Api'])
        self.assertEqual([p.name for p in inputs.main.sources], ['Api.cs'])
        self.assertTrue(inputs.main.options['implicitUsings'])
        self.assertEqual(len(inputs.records), 4)

    def test_cycles_and_root_escape(self):
        main = self.project('A/A.csproj', '<ProjectReference Include="../B/B.csproj"/>')
        self.project('B/B.csproj', '<ProjectReference Include="../A/A.csproj"/>')
        with self.assertRaisesRegex(ValueError, 'cyclic'): Inputs([main], self.root)
        self.project('A/A.csproj', '<ProjectReference Include="../../outside.csproj"/>')
        with self.assertRaisesRegex(ValueError, 'outside'): Inputs([main], self.root)

    def test_build_actions_and_conditions_rejected(self):
        main = self.write('A.csproj', '<Project><Target Name="Execute"/></Project>')
        with self.assertRaisesRegex(ValueError, 'build action'): Inputs([main], self.root)
        self.write('A.csproj', '<Project><PropertyGroup Condition="true"/></Project>')
        with self.assertRaisesRegex(ValueError, 'conditional'): Inputs([main], self.root)

    def test_symlink_and_ambiguous_directory(self):
        source = self.write('real.cs', 'class Real {}'); (self.root/'alias.cs').symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'symlink'): Inputs([self.root/'alias.cs'], self.root)
        self.project('A.csproj'); self.project('B.csproj')
        with self.assertRaisesRegex(ValueError, 'multiple'): Inputs([self.root], self.root)

    def test_offline_package_versions_and_closure(self):
        available = {'a': {'version':'1.0.0', 'dlls':['a.dll'], 'dependencies':[('b','[2.0.0,3.0.0)')]},
                     'b': {'version':'2.1.0', 'dlls':['b.dll'], 'dependencies':[]}}
        self.assertEqual(package_closure({'a':'1.0.0'}, available), ['a.dll','b.dll'])
        with self.assertRaisesRegex(ValueError, 'missing'): package_closure({'missing':'1.0.0'}, available)
        with self.assertRaisesRegex(ValueError, 'mismatch'): package_closure({'a':'2.0.0'}, available)
        available['b']['version']='3.0.0'
        with self.assertRaisesRegex(ValueError, 'mismatch'): package_closure({'a':'1.0.0'}, available)

    def test_packages_reject_build_tasks_and_traversal(self):
        path = self.root / 'bad.nupkg'
        for name in ['build/Run.targets', '../escape.dll']:
            with zipfile.ZipFile(path,'w') as archive: archive.writestr(name,b'x')
            with self.assertRaises(ValueError): packages([path],self.root/'packages')

if __name__ == '__main__': unittest.main()
