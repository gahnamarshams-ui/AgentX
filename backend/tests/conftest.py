import json
from types import SimpleNamespace


class ProviderResponse(SimpleNamespace):
    """Serialize test models to raw provider JSON; production must parse it."""
    @property
    def output_text(self):
        model = getattr(self, 'output_parsed', None)
        return json.dumps(model.model_dump()) if model is not None else ''
