"""Keep retrieval diagnostics in the audit, not in the model's prompt."""


def model_context(context: dict) -> dict:
    prompt = dict(context)
    prompt.pop("memory_selection", None)
    memory = dict(context["memory"])
    memory.pop("selection", None)
    for kind in ("corrections", "approved_guidance"):
        memory[kind] = []
        for source in context["memory"][kind]:
            item = dict(source)
            item.pop("match_scores", None)
            memory[kind].append(item)
    prompt["memory"] = memory
    return prompt
