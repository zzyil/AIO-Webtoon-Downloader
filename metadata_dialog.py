import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
from metadata_editor import read_metadata, update_metadata

class MetadataEditorDialog(tk.Toplevel):
    def __init__(self, parent, folder_path, primary_book, all_books):
        super().__init__(parent)
        self.title("Edit Metadata")
        self.geometry("500x600")
        self.folder_path = folder_path
        self.primary_book = primary_book
        self.all_books = all_books
        
        self.grab_set()
        
        self.meta_title = tk.StringVar()
        self.meta_authors = tk.StringVar()
        self.meta_artists = tk.StringVar()
        self.meta_genres = tk.StringVar()
        self.meta_publisher = tk.StringVar()
        self.meta_cover = tk.StringVar()
        self.apply_to_all = tk.BooleanVar(value=True)
        
        self._build_ui()
        self._load_initial()
        
    def _build_ui(self):
        pad = {'padx': 10, 'pady': 5}
        
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill='both', expand=True)
        
        ttk.Label(frame, text="Title:").grid(row=0, column=0, sticky='w', **pad)
        ttk.Entry(frame, textvariable=self.meta_title, width=40).grid(row=0, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Authors:").grid(row=1, column=0, sticky='w', **pad)
        ttk.Entry(frame, textvariable=self.meta_authors).grid(row=1, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Artists/Pencillers:").grid(row=2, column=0, sticky='w', **pad)
        ttk.Entry(frame, textvariable=self.meta_artists).grid(row=2, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Genres:").grid(row=3, column=0, sticky='w', **pad)
        ttk.Entry(frame, textvariable=self.meta_genres).grid(row=3, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Publisher:").grid(row=4, column=0, sticky='w', **pad)
        ttk.Entry(frame, textvariable=self.meta_publisher).grid(row=4, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Synopsis:").grid(row=5, column=0, sticky='nw', **pad)
        self.synopsis_text = tk.Text(frame, height=5, width=30)
        self.synopsis_text.grid(row=5, column=1, sticky='ew', **pad)
        
        ttk.Label(frame, text="Cover Image:").grid(row=6, column=0, sticky='w', **pad)
        c_frame = ttk.Frame(frame)
        c_frame.grid(row=6, column=1, sticky='ew', **pad)
        c_frame.columnconfigure(0, weight=1)
        ttk.Entry(c_frame, textvariable=self.meta_cover).grid(row=0, column=0, sticky='ew')
        ttk.Button(c_frame, text="Browse", command=self._browse_cover).grid(row=0, column=1)
        
        ttk.Checkbutton(frame, text="Apply to all chapters/volumes in this series", variable=self.apply_to_all).grid(row=7, column=0, columnspan=2, sticky='w', **pad)
        
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=8, column=0, columnspan=2, pady=15)
        self.save_btn = ttk.Button(btn_frame, text="Save", command=self._save)
        self.save_btn.pack(side='left', padx=5)
        self.cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self.destroy)
        self.cancel_btn.pack(side='left')
        self.progress = ttk.Progressbar(btn_frame, mode="indeterminate", length=150)
        
        frame.columnconfigure(1, weight=1)

    def _browse_cover(self):
        path = filedialog.askopenfilename(
            title="Select Cover Image",
            filetypes=[("Image Files", "*.jpg *.jpeg *.png *.webp *.gif")]
        )
        if path:
            self.meta_cover.set(path)
            
    def _load_initial(self):
        if not self.primary_book:
            return
        meta = read_metadata(self.primary_book)
        if "title" in meta: self.meta_title.set(meta["title"])
        if "writers" in meta: self.meta_authors.set(meta["writers"])
        if "pencillers" in meta: self.meta_artists.set(meta["pencillers"])
        if "genres" in meta: self.meta_genres.set(meta["genres"])
        if "publisher" in meta: self.meta_publisher.set(meta["publisher"])
        if "synopsis" in meta:
            self.synopsis_text.insert('1.0', meta["synopsis"])
            
    def _save(self):
        data = {
            "title": self.meta_title.get(),
            "writers": self.meta_authors.get(),
            "pencillers": self.meta_artists.get(),
            "genres": self.meta_genres.get(),
            "publisher": self.meta_publisher.get(),
            "synopsis": self.synopsis_text.get("1.0", "end-1c").strip()
        }
        cover_path = self.meta_cover.get()
        if cover_path and not os.path.exists(cover_path):
            messagebox.showerror("Error", "Cover image path does not exist.")
            return
            
        targets = self.all_books if self.apply_to_all.get() else [self.primary_book]
        targets = [t for t in targets if t and os.path.exists(t)]
        
        if not targets:
            messagebox.showinfo("Nothing to update", "No valid books found.")
            self.destroy()
            return
            
        self.save_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")
        self.progress.pack(side='left', padx=10)
        self.progress.start()
        
        def run_update():
            errors = []
            for t in targets:
                try:
                    update_metadata(t, data, cover_path if cover_path else None)
                except Exception as e:
                    errors.append(f"Failed on {os.path.basename(t)}: {e}")
            
            self.after(0, self._on_finish, errors)
            
        threading.Thread(target=run_update, daemon=True).start()
                
    def _on_finish(self, errors):
        self.progress.stop()
        self.progress.pack_forget()
        self.save_btn.config(state="normal")
        self.cancel_btn.config(state="normal")
        if errors:
            messagebox.showwarning("Errors during update", "\n".join(errors))
        else:
            messagebox.showinfo("Success", "Metadata updated successfully!")
        self.destroy()
