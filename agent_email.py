import os
import email
from email.header import decode_header
from html.parser import HTMLParser
from imapclient import IMAPClient
from dotenv import load_dotenv
from typing import Any
import logging

logger = logging.getLogger(__name__)

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []

    def handle_data(self, data):
        self.text_parts.append(data)

    def get_text(self) -> str:
        return " ".join(self.text_parts)

def strip_html_tags(html_content: str) -> str:
    parser = HTMLTextExtractor()
    try:
        parser.feed(html_content)
        return parser.get_text()
    except Exception:
        return html_content

def decode_mime_header(header_value: str) -> str:
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(encoding or "utf-8", errors="replace"))
            except Exception:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)

def fetch_unread_emails() -> list[dict[str, Any]]:
    imap_server = os.getenv("EMAIL_IMAP_SERVER")
    email_address = os.getenv("EMAIL_ADDRESS")
    email_password = os.getenv("EMAIL_APP_PASSWORD")
    
    if not all([imap_server, email_address, email_password]):
        raise ValueError("Missing IMAP environment configuration.")
        
    emails_data = []
    
    # Connect using IMAPClient
    with IMAPClient(imap_server, ssl=True) as client:
        client.login(email_address, email_password)
        client.select_folder("INBOX", readonly=True)
        
        # Search for unseen messages
        messages = client.search(["UNSEEN"])
        
        if not messages:
            return emails_data
            
        # Fetch RFC822 messages
        response = client.fetch(messages, ["RFC822"])
        
        for msg_id, data in response.items():
            try:
                raw_email = data.get(b"RFC822")
                if not raw_email:
                    continue
                msg = email.message_from_bytes(raw_email)
                
                sender = decode_mime_header(msg.get("From"))
                subject = decode_mime_header(msg.get("Subject"))
                
                body = ""
                attachments = []
                html_fallback = ""
                for part in msg.walk():
                    content_type = part.get_content_type()
                    filename = part.get_filename()
                    
                    # Handle attachments: any part with a filename parameter is an attachment
                    if filename:
                        payload = part.get_payload(decode=True)
                        import base64
                        preview = base64.b64encode(payload[:1024]).decode() if payload else ""
                        attachments.append({
                            "filename": decode_mime_header(filename),
                            "mime_type": content_type,
                            "content_base64": preview,
                            "size_bytes": len(payload) if payload else 0
                        })
                        continue
                    
                    content_disposition = str(part.get("Content-Disposition") or "")
                    if "attachment" in content_disposition.lower():
                        continue
                    
                    if part.is_multipart():
                        continue
                        
                    # Extract plain text body
                    if content_type == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    elif content_type == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_fallback = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if not body and html_fallback:
                    body = strip_html_tags(html_fallback)
                
                # Normalize whitespace and strip
                body_cleaned = " ".join(body.split())
                
                emails_data.append({
                    "uid": str(msg_id),
                    "sender": sender,
                    "subject": subject,
                    "body_snippet": body_cleaned,
                    "attachments": attachments,
                })
            except Exception as email_err:
                logger.error(f"Error parsing email UID {msg_id}: {email_err}", exc_info=True)
                
    return emails_data

def fetch_attachment_content_by_uid(uid: str, filename: str) -> bytes | None:
    """Fetch a specific attachment payload from an email identified by its IMAP UID."""
    imap_server = os.getenv("EMAIL_IMAP_SERVER")
    email_address = os.getenv("EMAIL_ADDRESS")
    email_password = os.getenv("EMAIL_APP_PASSWORD")
    if not all([imap_server, email_address, email_password]):
        raise ValueError("Missing IMAP environment configuration.")
    with IMAPClient(imap_server, ssl=True) as client:
        client.login(email_address, email_password)
        client.select_folder("INBOX", readonly=True)
        try:
            uid_int = int(uid)
        except (ValueError, TypeError):
            logger.error(f"Invalid UID provided: {uid}")
            return None
        resp = client.fetch([uid_int], ["RFC822"])
        if not resp or uid_int not in resp:
            return None
        raw_email = resp[uid_int][b"RFC822"]
        msg = email.message_from_bytes(raw_email)
        for part in msg.walk():
            if part.get_filename():
                fname = decode_mime_header(part.get_filename())
                if fname == filename:
                    return part.get_payload(decode=True)
    return None

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract all text from PDF bytes without page or character limits."""
    from io import BytesIO
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    text_parts = []
    for page in reader.pages:
        try:
            txt = page.extract_text()
            if txt:
                text_parts.append(txt)
        except Exception:
            continue
    return " ".join(text_parts)

if __name__ == "__main__":
    import argparse
    import sys

    load_dotenv()
    
    parser = argparse.ArgumentParser(description="Family Office Email Assistant CLI Tool")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")
    
    # Sub-command: check
    check_parser = subparsers.add_parser("check", help="Check and list unread emails")
    
    # Sub-command: fetch-attachments
    fetch_parser = subparsers.add_parser("fetch-attachments", help="Fetch and save attachments for a given email UID")
    fetch_parser.add_argument("uid", type=str, help="Email IMAP UID")
    fetch_parser.add_argument("--filename", type=str, default=None, help="Name of specific attachment to download (optional, downloads all if omitted)")
    fetch_parser.add_argument("-o", "--out-dir", type=str, default="./downloads", help="Directory where attachments will be saved (default: ./downloads)")
    
    # Sub-command: extract-pdf
    extract_parser = subparsers.add_parser("extract-pdf", help="Extract text from a PDF file")
    extract_parser.add_argument("file", type=str, help="Path to the PDF file")
    
    args = parser.parse_args()
    
    # Set up basic CLI logging to stdout
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    
    if args.command == "check" or not args.command:
        try:
            print("Checking unread emails...")
            unread_emails = fetch_unread_emails()
            print(f"Found {len(unread_emails)} unread email(s).\n")
            for idx, mail in enumerate(unread_emails, 1):
                print(f"[{idx}] UID: {mail['uid']}")
                print(f"    From:    {mail['sender']}")
                print(f"    Subject: {mail['subject']}")
                print(f"    Snippet: {mail['body_snippet'][:120]}...")
                if mail['attachments']:
                    print("    Attachments:")
                    for att in mail['attachments']:
                        print(f"      - {att['filename']} ({att['mime_type']}, {att.get('size_bytes', 0)} bytes)")
                else:
                    print("    Attachments: None")
                print("-" * 50)
        except Exception as e:
            print(f"Error checking emails: {e}", file=sys.stderr)
            sys.exit(1)
            
    elif args.command == "fetch-attachments":
        try:
            uid = args.uid
            # Ensure target directory exists
            out_dir = args.out_dir
            os.makedirs(out_dir, exist_ok=True)
            
            try:
                uid_int = int(uid)
            except (ValueError, TypeError):
                print(f"Error: UID must be an integer (got '{uid}').", file=sys.stderr)
                sys.exit(1)
                
            imap_server = os.getenv("EMAIL_IMAP_SERVER")
            email_address = os.getenv("EMAIL_ADDRESS")
            email_password = os.getenv("EMAIL_APP_PASSWORD")
            if not all([imap_server, email_address, email_password]):
                raise ValueError("Missing IMAP environment configuration.")
                
            print(f"Connecting to IMAP server to fetch email UID {uid}...")
            with IMAPClient(imap_server, ssl=True) as client:
                client.login(email_address, email_password)
                client.select_folder("INBOX", readonly=True)
                resp = client.fetch([uid_int], ["RFC822"])
                if not resp or uid_int not in resp:
                    print(f"Error: Email with UID {uid} not found.", file=sys.stderr)
                    sys.exit(1)
                
                raw_email = resp[uid_int][b"RFC822"]
                msg = email.message_from_bytes(raw_email)
                
                found_any = False
                for part in msg.walk():
                    raw_filename = part.get_filename()
                    if raw_filename:
                        fname = decode_mime_header(raw_filename)
                        # Secure filename to prevent path traversal per security rule
                        safe_filename = os.path.basename(fname)
                        if not safe_filename:
                            continue
                            
                        # If a specific filename was requested, check it
                        if args.filename and args.filename != fname:
                            continue
                            
                        found_any = True
                        payload = part.get_payload(decode=True)
                        if payload:
                            target_path = os.path.join(out_dir, safe_filename)
                            # Ensure safety check on target_path directory boundary
                            resolved_path = os.path.abspath(target_path)
                            resolved_out_dir = os.path.abspath(out_dir)
                            if not resolved_path.startswith(resolved_out_dir + os.path.sep) and resolved_path != resolved_out_dir:
                                print(f"Warning: Skipping file '{fname}' due to directory traversal protection.")
                                continue
                                
                            with open(target_path, "wb") as f:
                                f.write(payload)
                            print(f"Successfully downloaded and saved: {target_path} ({len(payload)} bytes)")
                        else:
                            print(f"Skipping empty attachment: {fname}")
                            
                if not found_any:
                    if args.filename:
                        print(f"Attachment '{args.filename}' not found in email UID {uid}.", file=sys.stderr)
                    else:
                        print(f"No attachments found in email UID {uid}.", file=sys.stderr)
        except Exception as e:
            print(f"Error fetching attachments: {e}", file=sys.stderr)
            sys.exit(1)
            
    elif args.command == "extract-pdf":
        try:
            file_path = args.file
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
            text = extract_pdf_text(pdf_bytes)
            if text:
                print(text)
            else:
                print("No text could be extracted from the PDF.")
        except Exception as e:
            print(f"Error extracting PDF text: {e}", file=sys.stderr)
            sys.exit(1)
