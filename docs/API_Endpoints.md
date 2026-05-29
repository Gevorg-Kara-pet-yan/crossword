# 🔗 API Endpoints
## 📌 List of Available API Endpoints

| URL                     | Method | Description                                         | Parameters / Data Format                                | Type  |
|-------------------------|--------|-----------------------------------------------------|--------------------------------------------------------|-------|
| /upload-bg              | POST   | Asynchronously uploads a background image          | multipart/form-data (bg_file field)                    | Async |
| /delete-bg              | POST   | Asynchronously deletes a background image          | JSON body: {"filename": "..."}                         | Async |
| /download/<id>          | GET    | Downloads the full crossword data in JSON format   | Path parameter: id (integer)                           | Async |
| /input                  | POST   | Creates a new crossword and saves it to the DB     | application/x-www-form-urlencoded or multipart/form-data | Async |
| /edit/<id>              | POST   | Updates an existing crossword                       | Path parameter: id, form data fields                   | Async |
| /delete_crossword/<id>  | POST   | Deletes a crossword from the DB                     | Path parameter: id (integer)                           | Async |
| /my-crosswords          | GET    | Displays paginated list of crosswords with search  | Query params: q (string), page (int)                   | Async |
