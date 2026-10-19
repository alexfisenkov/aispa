import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'skills/aispa/tools'
sys.path.insert(0, str(TOOLS))
from aispa_core import docmeta, packets
from aispa_core.store import Journal, Invalid


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(['git','init','-q',str(self.root)],check=True,capture_output=True)
        self.write('.gitignore', '.aispa/\nprivate/\n')
        self.store = Journal(self.root)
        self.state = self.store.create('navigation','Navigation fixture')
        for name in ('AGENTS.md','README.md','src/main.py','docs/input.md','docs/other.md'):
            self.write(name, '# Источник 🙂\n\n' + 'Описание и назначение документа. ' * 20)
            docmeta.register(self.store,self.state,name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self,name,text):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(text,encoding='utf8',newline='\n');return p

    def task(self, zones=None, reads=None):
        return {'id':'T01','title':'Fixture','requirements':[], 'dependsOn':[],
                'zones':zones or ['src/main.py'],'readZones':reads or ['docs/input.md'],
                'criteria':['Works'],'wave':1,'status':'pending'}

    def test_compact_has_navigation_only_and_detailed_keeps_all_provenance(self):
        before=copy.deepcopy(self.state);raw=(self.root/'README.md').read_bytes()
        small=docmeta.catalog(self.store,self.state);full=docmeta.catalog(self.store,self.state,detailed=True)
        self.assertEqual(small['format'],'compact');self.assertEqual(full['format'],'detailed')
        self.assertLess(len(json.dumps(small)), len(json.dumps(full))*0.7)
        for row in small['documents']:
            self.assertLessEqual(len(row['summary']),docmeta.MAX_NAVIGATION_SUMMARY)
            self.assertEqual(set(row),{'path','status','summary','summaryTruncated'})
        self.assertIn('sourceSha256',full['documents'][0]);self.assertIn('header',full['documents'][0])
        self.assertEqual(self.state,before);self.assertEqual((self.root/'README.md').read_bytes(),raw)
        self.write('README.md','# Changed\n\nOther content.')
        for detailed in (False,True):
            row=docmeta.catalog(self.store,self.state,paths=['README.md'],detailed=detailed)['documents'][0]
            self.assertEqual(row['status'],'stale');self.assertNotIn('summary',row)

    def test_exact_paths_omit_catalog_but_directories_keep_navigation(self):
        result=packets.document_context(self.store,self.state,'task',[self.task()])
        self.assertNotIn('documentCatalog',result)
        self.assertEqual(result['documentRouting'],{'catalog':'omitted','reason':'task-files'})
        self.assertNotIn('documentWarnings',result)
        broad=packets.document_context(self.store,self.state,'task',[self.task(zones=['src'])])
        self.assertEqual(broad['documentCatalog']['format'],'compact')
        self.assertEqual({r['path'] for r in broad['documentCatalog']['documents']},
                         {'AGENTS.md','src/main.py','docs/input.md'})
        missing=packets.document_context(self.store,self.state,'task',[self.task(zones=['src/new.py'])])
        self.assertIn('documentCatalog',missing)

    def test_known_paths_keep_warnings_beyond_navigation_limit(self):
        self.write('docs/other.md','# New body')
        with patch.object(docmeta,'MAX_DOCUMENTS',1):
            view=docmeta.catalog(self.store,self.state)
            self.assertEqual(len(view['documents']),1)
            result=packets.document_context(self.store,self.state,'task',
                    [self.task(reads=['docs/input.md','docs/other.md'])])
        self.assertNotIn('documentCatalog',result)
        self.assertEqual(result['documentWarnings'],[{'path':'docs/other.md','status':'stale'}])

    def test_explicit_sources_are_real_scoped_files_and_keep_common_warnings(self):
        self.write('AGENTS.md','# Changed rules')
        result=packets.document_context(self.store,self.state,'task',[self.task(zones=['src'])],
                                       ['src/main.py','docs/input.md'])
        self.assertEqual(result['documentRouting']['reason'],'explicit-files')
        self.assertEqual(result['documentWarnings'],[{'path':'AGENTS.md','status':'stale'}])
        for paths in ([],['../outside.md'],['src'],['src/missing.py'],['docs/other.md']):
            with self.subTest(paths=paths),self.assertRaises(Invalid):
                packets.document_context(self.store,self.state,'task',[self.task()],paths)
        self.write('private/hidden.md','Secret-free but ignored')
        self.write('.env','placeholder')
        for name in ('private/hidden.md','.env'):
            with self.assertRaises(Invalid):
                packets.document_context(self.store,self.state,'wave',[],[name])

    def test_unknown_metadata_and_outdated_inline_header_remain_visible(self):
        path=self.write('docs/new.md','# New\n\nUnknown metadata')
        result=packets.document_context(self.store,self.state,'task',[self.task(reads=['docs/new.md'])])
        self.assertEqual(result['documentWarnings'],[{'path':'docs/new.md','status':'unindexed'}])
        docmeta.register(self.store,self.state,'docs/new.md',expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         summary='Before',write_header=True)
        docmeta.register(self.store,self.state,'docs/new.md',expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),summary='After')
        row=docmeta.catalog(self.store,self.state,paths=['docs/new.md'])['documents'][0]
        self.assertTrue(row['headerNeedsRefresh']);self.assertNotIn('header',row)
        result=packets.document_context(self.store,self.state,'task',[self.task(reads=['docs/new.md'])])
        self.assertEqual(result['documentWarnings'],[{'path':'docs/new.md','status':'fresh','headerNeedsRefresh':True}])

    def test_catalog_budget_and_dependency_known_path_checks(self):
        with patch.object(docmeta,'MAX_NAVIGATION_CHARS',1):
            small=docmeta.catalog(self.store,self.state)
        self.assertFalse(small['documents']);self.assertEqual(small['omitted']['count'],len(self.state['documents']))
        parent=self.task(zones=['src'])
        child=self.task(zones=['README.md']);child['id']='T02';child['dependsOn']=['T01']
        result=packets.document_context(self.store,self.state,'task',[parent,child])
        self.assertIn('documentCatalog',result)
        self.assertEqual(packets.document_context(self.store,self.state,'coverage',[])['documentRouting']['reason'],'brief-and-spec')

    def test_dispatch_omission_does_not_change_contract_or_mandatory_fields(self):
        task=self.task(zones=['src']);self.state['tasks']=[task]
        self.state['taskHistory']={'T01':[{'id':'T01-A01','roles':{},'actors':{},'rounds':[]}]}
        class Pipeline:
            def trace_requirements(self,_):return []
            def inherited_proofs(self,_):return []
            def expected_checks(self,*_):return {('T01','Works')}
        before=packets.build(self.store,self.state,Pipeline(),'task','T01','critic2')
        after=packets.build(self.store,self.state,Pipeline(),'task','T01','critic2',source_paths=['src/main.py'])
        navigation={'documentCatalog','documentRouting','documentWarnings'}
        self.assertEqual({k:v for k,v in before.items() if k not in navigation},
                         {k:v for k,v in after.items() if k not in navigation})
        self.assertEqual(after['routing']['criticRounds'],2)
        self.assertEqual(after['expectedChecks'],[{'requirement':'T01','criterion':'Works'}])
        with self.assertRaises(Invalid):packets.build(self.store,self.state,None,'blind',source_paths=['README.md'])
