"""Application e-mail as a draft that the user reviews and sends. Nothing is ever sent.

Typical use from a GUI dialog::

    from reversehire import mail

    settings = mail.MailSettings.from_dict(saved)          # kept by the integrator
    recipient = mail.pick_recipient(contacts, company_email, typed)
    draft = mail.build_draft(settings, company=name, role=role,
                             contact_name=recipient.name if recipient else "",
                             to=recipient.email if recipient else "")
    environment = mail.detect_environment()                # read-only, fast
    methods = mail.recommended_methods(environment)        # available, best first
    infos = [m.info() for m in methods]                    # labels + explanations
    outcome = mail.method_by_id(methods, chosen).open_draft(draft)
    # or: outcome = mail.open_with_fallback(draft, methods, first=chosen)[-1]
    show(outcome.message_text(), outcome.step_texts())

Modules: compose (settings, templates, draft, recipient), urls (mailto/Gmail links),
eml (draft files), detect (which mail programs exist), methods (the ways to open a
draft and the fallback chain), outcome, texts (i18n keys with English text), system
(the replaceable side effects).
"""

from reversehire.mail.compose import (DEFAULT_TEMPLATES, PLACEHOLDERS, Draft, MailSettings, Recipient,
                                      build_draft, is_plausible_email, pick_recipient, recipient_domain,
                                      render_template, unknown_placeholders)
from reversehire.mail.detect import MailEnvironment, Probe, detect_environment
from reversehire.mail.eml import build_eml, write_eml
from reversehire.mail.methods import (AUTOMATIC, METHOD_IDS, MailMethod, MethodInfo, all_methods,
                                      automatic_method, describe_methods, method_by_id, open_with_fallback,
                                      recommended_methods)
from reversehire.mail.outcome import Outcome
from reversehire.mail.system import LaunchError, System
from reversehire.mail.texts import EN as TEXTS_EN
from reversehire.mail.texts import Text
from reversehire.mail.urls import gmail_compose_url, mailto_url

__all__ = [
    "DEFAULT_TEMPLATES", "PLACEHOLDERS", "Draft", "MailSettings", "Recipient", "build_draft",
    "is_plausible_email", "pick_recipient", "recipient_domain", "render_template", "unknown_placeholders",
    "MailEnvironment", "Probe", "detect_environment", "build_eml", "write_eml", "AUTOMATIC", "METHOD_IDS",
    "MailMethod", "automatic_method",
    "MethodInfo", "all_methods", "describe_methods", "method_by_id", "open_with_fallback",
    "recommended_methods", "Outcome", "LaunchError", "System", "TEXTS_EN", "Text", "gmail_compose_url",
    "mailto_url",
]
