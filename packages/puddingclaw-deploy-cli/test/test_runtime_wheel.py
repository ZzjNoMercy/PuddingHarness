import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec=importlib.util.spec_from_file_location('wheel_verifier',Path(__file__).resolve().parents[1]/'scripts/verify-runtime-wheel.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class WheelProof(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.stage=Path(self.temp.name);self.wheel=self.stage/'test.whl'
  (self.stage/'app.py').write_bytes(b'original')
  (self.stage/'pyproject.toml').write_text('[project]\nname="puddingharness-backend"\nversion="0.1.0"\n')
  (self.stage/'.stage-manifest.json').write_text(json.dumps({'python':{'files':[{'path':'app.py','sha256':hashlib.sha256(b'original').hexdigest()}]}}))
 def write(self,version='0.1.0',content=b'original',extra=None):
  with zipfile.ZipFile(self.wheel,'w') as z:
   z.writestr('puddingharness_backend-0.1.0.dist-info/METADATA',f'Name: puddingharness-backend\nVersion: {version}\n')
   z.writestr('app.py',content)
   if extra:z.writestr(*extra)
 def test_exact_payload(self):
  self.write();self.assertEqual(module.verify(self.stage,self.wheel)['python_files_verified'],1)
 def test_stale_metadata(self):
  self.write(version='9.9.9')
  with self.assertRaisesRegex(ValueError,'identity'):module.verify(self.stage,self.wheel)
 def test_stale_payload(self):
  self.write(content=b'stale')
  with self.assertRaisesRegex(ValueError,'payload'):module.verify(self.stage,self.wheel)
 def test_extra_python(self):
  self.write(extra=('old.py',b'extra'))
  with self.assertRaisesRegex(ValueError,'inventory'):module.verify(self.stage,self.wheel)
 def test_cli_compatibility_pin(self):
  self.write();(self.stage/'cli_runtime.py').write_text('CLI_VERSION = \"0.1.20-rc.1\"\n')
  self.assertEqual(module.verify(self.stage,self.wheel,'0.1.20-rc.1')['version'],'0.1.0')
  with self.assertRaisesRegex(ValueError,'compatibility'):module.verify(self.stage,self.wheel,'0.1.19')
 def test_traversal(self):
  self.write(extra=('../outside',b'extra'))
  with self.assertRaisesRegex(ValueError,'unsafe'):module.verify(self.stage,self.wheel)

if __name__=='__main__':unittest.main()
