"""unimem — UniMem Protocol client."""

__version__ = "1.0.0"
PROTOCOL_VERSION = "1.0.0"


def wrap_openai(client, **options):
    from unimem.completion_wrappers import wrap_openai as wrapper

    return wrapper(client, **options)


def wrap_anthropic(client, **options):
    from unimem.completion_wrappers import wrap_anthropic as wrapper

    return wrapper(client, **options)


def wrap_litellm(completion=None, **options):
    from unimem.completion_wrappers import wrap_litellm as wrapper

    return wrapper(completion, **options)
