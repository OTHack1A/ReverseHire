"""Export of the company card and of the application: document model, builders, renderers
(HTML, PDF, DOCX, XLSX) and the preview dialog.

    doc = build_company_card_document(card, tr=tr)
    doc = build_application_document(card, subject, body, cv_pdf_path, signature_name=name, tr=tr)
    ExportPreviewDialog(doc, parent, tr=tr).exec()

The dialog (and PySide6) is imported lazily so the builders and renderers stay usable
without Qt for DOCX/XLSX.
"""

from .builders import build_application_document, build_company_card_document
from .docx_render import render_docx
from .html_render import render_html
from .model import ExportDocument, ExportError, NoPrinterError
from .xlsx import render_xlsx


def __getattr__(name: str):
    if name == "ExportPreviewDialog":
        from .dialog import ExportPreviewDialog

        return ExportPreviewDialog
    if name in ("render_pdf", "print_pdf_file"):
        from . import pdf

        return getattr(pdf, name)
    raise AttributeError(name)


__all__ = [
    "build_application_document", "build_company_card_document", "ExportDocument", "ExportError",
    "NoPrinterError", "ExportPreviewDialog", "render_docx", "render_html", "render_pdf", "render_xlsx",
    "print_pdf_file",
]
