/*
  ResaleWise SG — front-end configuration. Loaded before api.js on every page.

  Where does the API live?
    - Leave this empty and it is auto-detected:
        * pages opened from localhost / 127.0.0.1 / a file  ->  http://localhost:5000
          (local dev: Apache/XAMPP or `python -m http.server` for the pages,
           `python app.py` for the API)
        * pages served from anywhere else                    ->  the same origin
          (the Flask app serves the pages itself — see render.yaml)
    - Set it only if the API is hosted on a DIFFERENT domain than the pages:
        window.RW_API_BASE = "https://your-api.example.com";
*/
window.RW_API_BASE = window.RW_API_BASE || "";
