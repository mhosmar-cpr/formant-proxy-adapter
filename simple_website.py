from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/test", methods=["GET"])
def hello_world_get():
    return jsonify({"return_message": "hello world"})


@app.route("/test", methods=["POST"])
def hello_world_post():
    return jsonify({"return_message": "goodye world"})
    
# main driver function
if __name__ == '__main__':
  
    # run() method of Flask class runs the application 
    # on the local development server.
    app.run()