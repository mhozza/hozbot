import os
import email
from email.header import decode_header
from html.parser import HTMLParser
from imapclient import IMAPClient
from dotenv import load_dotenv

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

def fetch_unread_emails() -> list[dict[str, str]]:
    load_dotenv()
    
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
            raw_email = data[b"RFC822"]
            msg = email.message_from_bytes(raw_email)
            
            sender = decode_mime_header(msg.get("From"))
            subject = decode_mime_header(msg.get("Subject"))
            
            body = ""
            attachments = []
            if msg.is_multipart():
                html_fallback = ""
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    filename = part.get_filename()
                    # Handle attachments
                    if "attachment" in content_disposition.lower():
                        payload = part.get_payload(decode=True)
                        if payload:
                            # Limit attachment size to 1KB for preview, encode as base64
                            import base64
                            preview = base64.b64encode(payload[:1024]).decode()
                            attachments.append({
                                "filename": decode_mime_header(filename) if filename else "unknown",
                                "mime_type": content_type,
                                "content_base64": preview,
                            })
                        continue
                    # Extract plain text body
                    if content_type == "text/plain" and "attachment" not in content_disposition.lower():
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break
                    elif content_type == "text/html" and "attachment" not in content_disposition.lower():
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_fallback = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if not body and html_fallback:
                    body = strip_html_tags(html_fallback)
            else:
                content_type = msg.get_content_type()
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    if content_type == "text/html":
                        body = strip_html_tags(payload.decode(charset, errors="replace"))
                    else:
                        body = payload.decode(charset, errors="replace")
            
            # Normalize whitespace and strip
            body_cleaned = " ".join(body.split())
            
            emails_data.append({
                "sender": sender,
                "subject": subject,
                "body_snippet": body_cleaned,
                "attachments": attachments,
            })
            
    return emails_data

if __name__ == "__main__":
    # Simple self-test code when run directly
    try:
        print("Fetching emails...")
        unread = fetch_unread_emails()
        print(f"Fetched {len(unread)} unread emails.")
        for idx, mail in enumerate(unread, 1):
            print(f"\n[{idx}] From: {mail['sender']}\nSubject: {mail['subject']}\nSnippet: {mail['body_snippet']}")
    except Exception as e:
        print(f"Failed to fetch emails: {e}")
