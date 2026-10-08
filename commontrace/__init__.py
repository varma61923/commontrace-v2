"""commontrace — CommonTrace Protocol client."""

__version__ = "2.0.0"
PROTOCOL_VERSION = "2.0.0"


def wrap_openai(client, **options):
    from commontrace.completion_wrappers import wrap_openai as wrapper

    return wrapper(client, **options)


def wrap_anthropic(client, **options):
    from commontrace.completion_wrappers import wrap_anthropic as wrapper

    return wrapper(client, **options)


def wrap_litellm(completion=None, **options):
    from commontrace.completion_wrappers import wrap_litellm as wrapper

    return wrapper(completion, **options)
