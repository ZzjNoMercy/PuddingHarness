"""Run with a staged noneditable Python from outside the repository."""
import base64
import json
from pathlib import Path
import tempfile

from langchain_core.tools import ToolException
import provider_registry
import tools.read_resource_tool as resource


def main():
    for module in (provider_registry, resource):
        assert 'site-packages' in Path(module.__file__).parts, module.__file__
    with tempfile.TemporaryDirectory() as directory:
        registry = provider_registry.ProviderRegistry(Path(directory))
        payload = provider_registry._default_registry()
        assert not {'vanna_llm', 'vanna_embedding'} & payload['bindings'].keys()
        payload['bindings']['vanna_llm'] = payload['bindings']['agent']
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(payload).encode()
        registry.path.write_bytes(original)
        assert 'vanna_llm' not in registry._payload()['bindings']
        for binding in ('vanna_llm', 'vanna_embedding', 'unknown_workload'):
            try:
                registry.set_binding(binding, payload['bindings']['agent'])
            except ValueError:
                pass
            else:
                raise AssertionError('retired or unknown binding accepted')
        assert registry.path.read_bytes() == original
    read = resource.ReadResourceTool._resource_text
    def blob(value, mime='text/plain'):
        return {'contents': [{'blob': value, 'mimeType': mime}]}
    assert read(blob(base64.b64encode('中文 resource'.encode()).decode())) == '中文 resource'
    for value in ('not-base64!', base64.b64encode(b'\xff').decode()):
        try:
            read(blob(value))
        except ToolException:
            pass
        else:
            raise AssertionError('invalid text accepted')
    binary = base64.b64encode(b'\x00\xff').decode()
    assert read(blob(binary, 'application/octet-stream')) == binary
    resource.MAX_MCP_RESOURCE_TEXT_BYTES = 4
    try:
        read(blob(base64.b64encode(b'a' * 20).decode()))
    except ToolException:
        pass
    else:
        raise AssertionError('oversized text accepted')
    print('PASS: installed bindings and MCP resource decoding; site-packages origins verified')


if __name__ == '__main__':
    main()
