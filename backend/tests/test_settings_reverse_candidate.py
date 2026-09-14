import json
import pytest
from harness.source_snapshot import _inventory,_encoded,_sha
from harness.session_import import encoded
from test_session_reverse import roots,run,pytestmark

def configure(roots):
    source=roots[0]
    (source/'payload/config.json').write_bytes(encoded({'schema_version':1,'cache':{'enabled':False},'knowledge':{'token':'source-fixture-secret'}}))
    plan=json.loads((source/'plan.json').read_bytes());plan['inventory']=_inventory(source/'payload')
    manifest=json.loads((source/'manifest.json').read_bytes());manifest['inventory']=plan['inventory'];manifest['plan_digest']=_sha(_encoded(plan))
    (source/'plan.json').write_bytes(_encoded(plan));(source/'manifest.json').write_bytes(_encoded(manifest))
    (roots[1]/'config.json').write_bytes(encoded({'schema_version':1,'cache':{'enabled':False}}))
    (roots[2]/'config.json').write_bytes(encoded({'schema_version':1,'cache':{'enabled':True}}))

def test_combined_session_and_settings_reverse_is_idempotent_and_redacted(roots):
    configure(roots);result=run(roots,include_generic_settings=True)
    assert result['generic_settings_reversed'] and result['changed_settings_sections']==['cache']
    value=json.loads((roots[-1]/'payload/config.json').read_bytes())
    assert value=={'schema_version':1,'cache':{'enabled':True},'knowledge':{'token':'source-fixture-secret'}}
    assert 'source-fixture-secret' not in (roots[-1]/'manifest.json').read_text()
    assert run(roots,include_generic_settings=True)==dict(result,idempotent=True)
    with pytest.raises(ValueError):run(roots)

def test_target_settings_change_during_copy_cannot_commit(roots):
    configure(roots)
    def change(name):(roots[2]/'config.json').write_bytes(b'{}')
    with pytest.raises(ValueError):run(roots,include_generic_settings=True,_after_copy=change)
    assert json.loads((roots[-1]/'manifest.json').read_bytes())['state']=='copying'

def test_baseline_settings_conflict_rejected_before_freeze(roots):
    configure(roots);(roots[1]/'config.json').write_bytes(b'{}')
    with pytest.raises(ValueError):run(roots,include_generic_settings=True)
    assert not (roots[2]/'.installation-freeze-v1.json').exists()

def test_completed_generated_settings_not_repaired(roots):
    configure(roots);run(roots,include_generic_settings=True)
    p=roots[-1]/'payload/config.json';p.write_bytes(b'{}')
    with pytest.raises(ValueError):run(roots,include_generic_settings=True)
    assert p.read_bytes()==b'{}'
