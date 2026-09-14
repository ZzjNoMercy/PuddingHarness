import json
import pytest
from harness.settings_reverse import reverse_generic_settings
from harness.session_import import encoded

def test_merge_generic_sections_preserves_source_credentials_and_unknown_keys():
    source={'schema_version':1,'cache':{'enabled':False},'llm':{'key':'source-secret'},'knowledge':{'url':'old'}}
    before={'schema_version':1,'cache':{'enabled':False},'server':{'port':8000}}
    after={'schema_version':1,'cache':{'enabled':True},'server':{'port':8000}}
    raw,receipt=reverse_generic_settings(*map(encoded,(source,before,after)))
    assert json.loads(raw)==dict(source,cache={'enabled':True})
    assert receipt['changed_sections']==['cache'] and not receipt['credential_continuity_verified']
    assert 'source-secret' not in json.dumps(receipt) and 'knowledge' not in json.dumps(receipt)

def test_section_deletion_is_real_and_unknown_source_section_is_preserved():
    raw,receipt=reverse_generic_settings(encoded({'cache':{'enabled':False},'knowledge':{}}),encoded({'cache':{'enabled':False}}),b'{}')
    assert json.loads(raw)=={'knowledge':{}} and receipt['changed_sections']==['cache']

@pytest.mark.parametrize('before,after',[(b'{}',b'{}'),(b'{"cache":{"enabled":false}}',b'{"cache":{"enabled":true},"llm":{"key":"new"}}')])
def test_conflicting_baseline_or_unsupported_target_change_rejected(before,after):
    with pytest.raises(ValueError):reverse_generic_settings(b'{"cache":{"enabled":false}}',before,after)

@pytest.mark.parametrize('raw',[b'{"cache":{},"cache":{}}',b'{"schema_version":true}',b'{"cache":null}',b'{"x":NaN}',b'[]'])
def test_invalid_settings_rejected(raw):
    with pytest.raises(ValueError):reverse_generic_settings(raw,b'{}',b'{}')
