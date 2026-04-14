import os
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils

# Make sure we use pypdf if available
try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def escape(s):
    return saxutils.escape(s) if s else ""

# ---------------------------------------------------------
# CBZ Handle
# ---------------------------------------------------------

def read_cbz_metadata(path: str) -> dict:
    meta = {}
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            if "ComicInfo.xml" in zf.namelist():
                xml_data = zf.read("ComicInfo.xml")
                root = ET.fromstring(xml_data)
                
                # Basic fields
                def get_text(tag):
                    el = root.find(tag)
                    return el.text if el is not None and el.text else ""
                    
                meta["title"] = get_text("Title")
                meta["synopsis"] = get_text("Summary")
                meta["writers"] = get_text("Writer")
                meta["pencillers"] = get_text("Penciller")
                meta["genres"] = get_text("Genre")
                meta["publisher"] = get_text("Publisher")
    except Exception:
        pass
    return meta

def update_cbz_metadata(path: str, data: dict, cover_path: str = None):
    temp_fd, temp_path = tempfile.mkstemp(suffix=".cbz")
    os.close(temp_fd)
    
    try:
        with zipfile.ZipFile(path, 'r') as zin, zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            comic_info_found = False
            for item in zin.infolist():
                if item.filename.lower() == "comicinfo.xml":
                    comic_info_found = True
                    xml_data = zin.read(item.filename)
                    try:
                        root = ET.fromstring(xml_data)
                    except Exception:
                        root = ET.Element("ComicInfo")
                else:
                    if cover_path and item.filename.startswith("0000_cover"):
                        continue  # skip old cover if we are replacing it
                    zout.writestr(item, zin.read(item.filename))
            
            if not comic_info_found:
                # Create a new root if it didn't exist
                root = ET.Element("ComicInfo", {
                    "xmlns:xsd": "http://www.w3.org/2001/XMLSchema", 
                    "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"
                })
            
            # Helper to update XML
            def set_text(tag, value):
                el = root.find(tag)
                if value is not None and value.strip():
                    if el is None:
                        el = ET.SubElement(root, tag)
                    el.text = value.strip()
                elif el is not None:
                    root.remove(el)

            if "title" in data: set_text("Title", data["title"])
            if "title" in data: set_text("Series", data["title"])
            if "synopsis" in data: set_text("Summary", data["synopsis"])
            if "writers" in data: set_text("Writer", data["writers"])
            if "pencillers" in data: set_text("Penciller", data["pencillers"])
            if "genres" in data: set_text("Genre", data["genres"])
            if "publisher" in data: set_text("Publisher", data["publisher"])

            xml_str = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
            zout.writestr("ComicInfo.xml", xml_str)

            if cover_path and os.path.exists(cover_path):
                ext = os.path.splitext(cover_path)[1]
                zout.write(cover_path, f"0000_cover{ext}")
                
        shutil.move(temp_path, path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e

# ---------------------------------------------------------
# EPUB Handle
# ---------------------------------------------------------

def _find_epub_opf(zin: zipfile.ZipFile) -> str:
    try:
        container = zin.read("META-INF/container.xml")
        root = ET.fromstring(container)
        ns = {"ns": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rootfile = root.find(".//ns:rootfile", ns)
        if rootfile is not None:
            return rootfile.attrib.get("full-path", "EPUB/content.opf")
    except Exception:
        pass
    return "EPUB/content.opf"

def read_epub_metadata(path: str) -> dict:
    meta = {}
    try:
        with zipfile.ZipFile(path, 'r') as zf:
            opf_path = _find_epub_opf(zf)
            if opf_path in zf.namelist():
                opf_data = zf.read(opf_path)
                root = ET.fromstring(opf_data)
                
                ns = {
                    "opf": "http://www.idpf.org/2007/opf",
                    "dc": "http://purl.org/dc/elements/1.1/"
                }
                
                metadata = root.find("opf:metadata", ns)
                if metadata is not None:
                    title_el = metadata.find("dc:title", ns)
                    desc_el = metadata.find("dc:description", ns)
                    creator_el = metadata.findall("dc:creator", ns)
                    subj_el = metadata.findall("dc:subject", ns)
                    pub_el = metadata.find("dc:publisher", ns)
                    
                    if title_el is not None: meta["title"] = title_el.text
                    if desc_el is not None: meta["synopsis"] = desc_el.text
                    if creator_el: meta["writers"] = ", ".join([c.text for c in creator_el if c.text])
                    if subj_el: meta["genres"] = ", ".join([s.text for s in subj_el if s.text])
                    if pub_el is not None: meta["publisher"] = pub_el.text

    except Exception:
        pass
    return meta

def update_epub_metadata(path: str, data: dict, cover_path: str = None):
    temp_fd, temp_path = tempfile.mkstemp(suffix=".epub")
    os.close(temp_fd)
    
    try:
        with zipfile.ZipFile(path, 'r') as zin, zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            opf_path = _find_epub_opf(zin)
            
            for item in zin.infolist():
                if item.filename == "mimetype":
                    # Must be uncompressed
                    zout.writestr(item, zin.read(item.filename), compress_type=zipfile.ZIP_STORED)
                elif item.filename == opf_path:
                    opf_data = zin.read(item.filename)
                    
                    # Instead of full ElementTree parsing which might destroy exact namespace prefixes,
                    # we will do some basic replacements or register namespaces if needed.
                    # Since rewriting OPF perfectly with ET can be tricky due to namespaces, let's use ET properly.
                    ET.register_namespace("", "http://www.idpf.org/2007/opf")
                    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
                    ET.register_namespace("opf", "http://www.idpf.org/2007/opf")
                    
                    try:
                        root = ET.fromstring(opf_data)
                        ns = {
                            "opf": "http://www.idpf.org/2007/opf",
                            "dc": "http://purl.org/dc/elements/1.1/"
                        }
                        metadata_node = root.find("opf:metadata", ns)
                        if metadata_node is not None:
                            # Helper to update
                            def update_tag(tag_name, tag_ns_prefix, tag_ns_url, new_val):
                                # remove old
                                for e in metadata_node.findall(f"{tag_ns_prefix}:{tag_name}", ns):
                                    metadata_node.remove(e)
                                if new_val and new_val.strip():
                                    el = ET.SubElement(metadata_node, f"{{{tag_ns_url}}}{tag_name}")
                                    el.text = new_val.strip()

                            if "title" in data:
                                update_tag("title", "dc", "http://purl.org/dc/elements/1.1/", data["title"])
                            if "synopsis" in data:
                                update_tag("description", "dc", "http://purl.org/dc/elements/1.1/", data["synopsis"])
                            if "writers" in data:
                                update_tag("creator", "dc", "http://purl.org/dc/elements/1.1/", data["writers"])
                            if "genres" in data:
                                update_tag("subject", "dc", "http://purl.org/dc/elements/1.1/", data["genres"])
                            if "publisher" in data:
                                update_tag("publisher", "dc", "http://purl.org/dc/elements/1.1/", data["publisher"])

                        xml_str = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
                        zout.writestr(item.filename, xml_str)
                    except Exception as e:
                        # Fallback if parsing fails
                        zout.writestr(item, zin.read(item.filename))
                        
                elif cover_path and item.filename == "EPUB/images/cover.jpg":
                    # skip
                    pass
                else:
                    zout.writestr(item, zin.read(item.filename))
            
            if cover_path and os.path.exists(cover_path):
                # We expect the EPUB usually holds images/cover.jpg
                zout.write(cover_path, "EPUB/images/cover.jpg")
                
        shutil.move(temp_path, path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e

# ---------------------------------------------------------
# PDF Handle
# ---------------------------------------------------------

def read_pdf_metadata(path: str) -> dict:
    meta = {}
    if not HAS_PYPDF:
        return meta
    try:
        reader = PdfReader(path)
        pdf_meta = reader.metadata
        if pdf_meta:
            meta["title"] = pdf_meta.get("/Title", "")
            meta["authors"] = pdf_meta.get("/Author", "")
            meta["synopsis"] = pdf_meta.get("/Subject", "")
    except Exception:
        pass
    return meta

def update_pdf_metadata(path: str, data: dict, cover_path: str = None):
    if not HAS_PYPDF:
        return
        
    temp_fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(temp_fd)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        
        pdf_meta = {}
        if reader.metadata:
            pdf_meta.update(reader.metadata)
            
        if "title" in data: pdf_meta["/Title"] = data["title"]
        if "writers" in data: pdf_meta["/Author"] = data["writers"]
        if "synopsis" in data: pdf_meta["/Subject"] = data["synopsis"]
        
        writer.add_metadata(pdf_meta)
        
        start_idx = 0
        
        # Inject cover as first page
        if cover_path and os.path.exists(cover_path) and HAS_PIL:
            # We skip the original first page assuming it was a cover
            if len(reader.pages) > 0:
                start_idx = 1
                
            cover_pdf_path = cover_path + ".pdf"
            try:
                with Image.open(cover_path) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(cover_pdf_path, "PDF", resolution=100.0)
                
                try:
                    cover_reader = PdfReader(cover_pdf_path)
                    writer.add_page(cover_reader.pages[0])
                except Exception:
                    start_idx = 0  # Revert skip if cover failed to add
            finally:
                if os.path.exists(cover_pdf_path):
                    os.remove(cover_pdf_path)

        for i in range(start_idx, len(reader.pages)):
            writer.add_page(reader.pages[i])
            
        with open(temp_path, "wb") as f:
            writer.write(f)
            
        shutil.move(temp_path, path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e

# ---------------------------------------------------------
# Generic Router
# ---------------------------------------------------------

def read_metadata(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cbz":
        return read_cbz_metadata(path)
    elif ext == ".epub":
        return read_epub_metadata(path)
    elif ext == ".pdf":
        return read_pdf_metadata(path)
    return {}

def update_metadata(path: str, data: dict, cover_path: str = None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cbz":
        update_cbz_metadata(path, data, cover_path)
    elif ext == ".epub":
        update_epub_metadata(path, data, cover_path)
    elif ext == ".pdf":
        update_pdf_metadata(path, data, cover_path)
