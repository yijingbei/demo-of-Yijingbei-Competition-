"""Policy view of contacts suitable for existing platform/facility logic."""


def without_flight_contacts(observation):
    """Retain legacy unknown categories and leave the public observation intact."""
    return {
        **observation,
        **{
            field: [contact for contact in observation.get(field, ())
                    if contact.get("estimated_contact_kind") != "flight"]
            for field in ("organic_contacts", "shared_contacts")
        },
    }
