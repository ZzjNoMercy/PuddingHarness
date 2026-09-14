"""Reverse validated generic settings without claiming credential continuity."""
import hashlib
import json
from harness.settings_import import OWNED,MAX_CONFIG_BYTES,_object,_shape,project_settings
from harness.session_import import encoded


def _parse(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_CONFIG_BYTES:
        raise ValueError('Settings input exceeds budget')
    value=json.loads(raw,object_pairs_hook=_object)
    if not isinstance(value,dict):raise ValueError('Settings input must be an object')
    _shape(value)
    return value


def reverse_generic_settings(source_raw,before_raw,after_raw):
    source,before,after=map(_parse,(source_raw,before_raw,after_raw))
    source_projection,_,_=project_settings(source_raw)
    before_projection,_,_=project_settings(before_raw)
    after_projection,_,_=project_settings(after_raw)
    if source_projection!=before_projection:
        raise ValueError('Generic settings baseline differs from source')
    foreign=lambda value:{k:v for k,v in value.items() if k not in OWNED and k!='schema_version'}
    if encoded(foreign(before))!=encoded(foreign(after)):
        raise ValueError('Unsupported target settings changed; separate reverse migration required')
    selected=json.loads(after_projection)
    merged={k:v for k,v in source.items() if k not in OWNED}
    for key in OWNED:
        if key in selected:merged[key]=selected[key]
    payload=encoded(merged)
    if len(payload)>MAX_CONFIG_BYTES:raise ValueError('Merged settings exceed budget')
    # Revalidate the actual generated document, not only the target projection.
    if project_settings(payload)[0]!=after_projection:
        raise ValueError('Generated generic settings do not match target')
    sha=lambda raw:hashlib.sha256(raw).hexdigest()
    old=json.loads(before_projection)
    changed=[k for k in OWNED if (k in old)!=(k in selected) or encoded(old.get(k))!=encoded(selected.get(k))]
    return payload,{'format':'puddingharness-generic-settings-reverse/v1',
                    'source_sha256':sha(source_raw),'baseline_sha256':sha(before_raw),
                    'target_sha256':sha(after_raw),'payload_sha256':sha(payload),
                    'changed_sections':changed,'credential_continuity_verified':False}
