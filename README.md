# BillBear — Smart Bill Splitting Made Simple

BillBear is a mobile-friendly web application that helps groups split restaurant bills fairly. The app extracts items using OCR and allows each person to select what they consumed. Tax, service charges, and discounts are distributed accurately to ensure a fair split for everyone.

Live Website: https://billbear.me  
Repository: https://github.com/r-abhinav1/BillBear

---

## Overview

- Scan receipts or manually enter items
- Select items per person with real-time collaboration
- Split the bill fairly with tax, service charges, and discounts included
- Export the final split as a downloadable PDF
- Works seamlessly on mobile devices
- Securely deployed on Vercel with persistent data storage using MongoDB Atlas

---

## Screenshot

---<img width="1920" height="1080" alt="Screenshot 2025-10-24 105641" src="https://github.com/user-attachments/assets/18540920-57b6-45eb-8316-e94577d4dccc" />


## Features

### Receipt OCR
- Upload receipt images to detect items automatically
- Manual editing supported for accuracy

### Room Creation & Collaboration
- Create a room with a unique code and share via link or QR
- Track who joined and submitted in real time
- Host can force-complete session if needed

### Fair Cost Distribution
- Items shared only among selected users
- Tax, service charges, and discounts proportionally distributed
- Per-user breakdown shown in results

### PDF Export
- Clean PDF summary of bill split downloadable by users

### SEO & PWA Support
- Dynamic sitemaps and metadata for discoverability
- PWA-enabled for app-like mobile experience

---

## Tech Stack

| Component | Technology |
|----------|------------|
| Backend | Flask |
| Frontend | HTML, CSS, JavaScript |
| Database | MongoDB Atlas |
| OCR | Google Gemini Flash 2.0 |
| Deployment | Vercel |

---

## Installation (Local Development)

```bash
git clone https://github.com/r-abhinav1/BillBear.git
cd BillBear
pip install -r requirements.txt
python app.py
# or
flask run
