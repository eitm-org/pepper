import wget
import sys
import os
from modules.python.ImageGenerationUI import UserInterfaceSupport
from modules.python.TextColor import TextColor


def download_models(output_dir):
    output_dir = UserInterfaceSupport.handle_output_directory(output_dir)
    sys.stderr.write(TextColor.YELLOW + "DOWNLOADING MODEL DESCRIPTION FILE" + TextColor.END + "\n")
    description_file = "https://storage.googleapis.com/kishwar-helen/models_pepper/pepper_model_description.csv"
    wget.download(description_file, output_dir)
    sys.stderr.write("\n")
    sys.stderr.flush()

    with open(output_dir+'pepper_model_description.csv') as f:
        models = [line.rstrip() for line in f]

    os.remove(output_dir+'pepper_model_description.csv')

    for model in models:
        model_name, model_url = model.split(',')
        if os.path.exists(output_dir+model_name+".pkl"):
            sys.stderr.write("INFO: MODEL EXISTS " + str(model_name) + ".pkl" + "\n")
            continue
        sys.stderr.write("INFO: DOWNLOADING FILE: " + str(model_name) + ".pkl\n")
        sys.stderr.write("INFO: DOWNLOADING LINK: " + str(model_url) + "\n")
        wget.download(model_url, output_dir)
        sys.stderr.write("\n")
        sys.stderr.flush()
