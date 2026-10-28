"""
VTU Marks & Equivalent Credits Calculator
Run this file to start the web application:
    python new.py
Then open http://localhost:5000 in your browser.
"""

from app import app

if __name__ == '__main__':
    print("=" * 60)
    print("VTU Marks & Credit Calculator Server")
    print("Range: 1JT23CS001 to 1JT23CS193")
    print("Formula: Total Marks = sum(marks_obtained * credits)")
    print("Running on http://localhost:5000")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=True)
