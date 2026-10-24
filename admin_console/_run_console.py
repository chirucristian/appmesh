"""Local launcher: run the console single-process (no debug reloader fork)."""
import app

app.bootstrap()  # ensure DynamoDB tables, seed root user, load deployments
app.app.run(debug=False, port=8081, threaded=True, use_reloader=False)
