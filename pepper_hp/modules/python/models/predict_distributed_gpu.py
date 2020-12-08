import sys
import os
import torch
import time
import torch.nn as nn
from torch.utils.data import DataLoader
import concurrent.futures
from torch.nn.parallel import DistributedDataParallel
from datetime import datetime

from pepper_hp.modules.python.models.dataloader_predict import SequenceDataset
from pepper_hp.modules.python.models.ModelHander import ModelHandler
from pepper_hp.modules.python.Options import ImageSizeOptions, TrainOptions
from pepper_hp.modules.python.DataStorePredict import DataStore
os.environ['PYTHONWARNINGS'] = 'ignore:semaphore_tracker:UserWarning'


def predict(input_filepath, file_chunks, output_filepath, model_path, batch_size, num_workers, theads_per_caller, device_id, rank):
    transducer_model, hidden_size, gru_layers, prev_ite = \
        ModelHandler.load_simple_model_for_training(model_path,
                                                    input_channels=ImageSizeOptions.IMAGE_CHANNELS,
                                                    image_features=ImageSizeOptions.IMAGE_HEIGHT,
                                                    seq_len=ImageSizeOptions.SEQ_LENGTH,
                                                    num_classes=ImageSizeOptions.TOTAL_LABELS)
    transducer_model.eval()
    transducer_model = transducer_model.eval()
    # create output file
    output_filename = output_filepath + "pepper_prediction_" + str(rank) + ".hdf"
    prediction_data_file = DataStore(output_filename, mode='w')

    # data loader
    input_data = SequenceDataset(input_filepath, file_chunks)
    data_loader = DataLoader(input_data,
                             batch_size=batch_size,
                             shuffle=False,
                             num_workers=num_workers)
    torch.set_num_threads(theads_per_caller)

    torch.cuda.set_device(device_id)
    transducer_model.to(device_id)
    transducer_model.eval()
    transducer_model = DistributedDataParallel(transducer_model, device_ids=[device_id])

    batch_completed = 0
    total_batches = len(data_loader)
    with torch.no_grad():
        for contig, contig_start, contig_end, chunk_id, images_hp1, images_hp2, position, index, ref_seq in data_loader:
            sys.stderr.flush()
            images_hp1 = images_hp1.type(torch.FloatTensor)
            images_hp2 = images_hp2.type(torch.FloatTensor)
            hidden_hp1 = torch.zeros(images_hp1.size(0), 2 * TrainOptions.GRU_LAYERS, TrainOptions.HIDDEN_SIZE)
            hidden_hp2 = torch.zeros(images_hp2.size(0), 2 * TrainOptions.GRU_LAYERS, TrainOptions.HIDDEN_SIZE)

            prediction_base_tensor_hp1 = torch.zeros((images_hp1.size(0), images_hp1.size(1), ImageSizeOptions.TOTAL_LABELS))
            prediction_base_tensor_hp2 = torch.zeros((images_hp2.size(0), images_hp2.size(1), ImageSizeOptions.TOTAL_LABELS))

            images_hp1 = images_hp1.to(device_id)
            images_hp2 = images_hp2.to(device_id)
            hidden_hp1 = hidden_hp1.to(device_id)
            hidden_hp2 = hidden_hp2.to(device_id)
            prediction_base_tensor_hp1 = prediction_base_tensor_hp1.to(device_id)
            prediction_base_tensor_hp2 = prediction_base_tensor_hp2.to(device_id)

            for i in range(0, ImageSizeOptions.SEQ_LENGTH, TrainOptions.WINDOW_JUMP):
                if i + TrainOptions.TRAIN_WINDOW > ImageSizeOptions.SEQ_LENGTH:
                    break
                chunk_start = i
                chunk_end = i + TrainOptions.TRAIN_WINDOW
                # chunk all the data
                image_chunk_hp1 = images_hp1[:, chunk_start:chunk_end]
                image_chunk_hp2 = images_hp2[:, chunk_start:chunk_end]

                # run inference
                output_base_hp1, hidden_hp1 = transducer_model(image_chunk_hp1, hidden_hp1)
                output_base_hp2, hidden_hp2 = transducer_model(image_chunk_hp2, hidden_hp2)

                # now calculate how much padding is on the top and bottom of this chunk so we can do a simple
                # add operation
                top_zeros = chunk_start
                bottom_zeros = ImageSizeOptions.SEQ_LENGTH - chunk_end

                # do softmax and get prediction
                # we run a softmax a padding to make the output tensor compatible for adding
                inference_layers = nn.Sequential(
                    nn.Softmax(dim=2),
                    nn.ZeroPad2d((0, 0, top_zeros, bottom_zeros))
                )
                inference_layers = inference_layers.to(device_id)

                # run the softmax and padding layers
                base_prediction_hp1 = (inference_layers(output_base_hp1) * 10).type(torch.IntTensor).to(device_id)
                base_prediction_hp2 = (inference_layers(output_base_hp2) * 10).type(torch.IntTensor).to(device_id)

                # now simply add the tensor to the global counter
                prediction_base_tensor_hp1 = torch.add(prediction_base_tensor_hp1, base_prediction_hp1)
                prediction_base_tensor_hp2 = torch.add(prediction_base_tensor_hp2, base_prediction_hp2)

                del inference_layers
                torch.cuda.empty_cache()

            # base_values, base_labels = torch.max(prediction_base_tensor, 2)

            # predicted_base_labels = base_labels.cpu().numpy()

            prediction_base_tensor_hp1 = prediction_base_tensor_hp1.cpu().numpy().astype(int)
            prediction_base_tensor_hp2 = prediction_base_tensor_hp2.cpu().numpy().astype(int)

            for i in range(images_hp1.size(0)):
                prediction_data_file.write_prediction(contig[i],
                                                      contig_start[i],
                                                      contig_end[i],
                                                      chunk_id[i],
                                                      position[i],
                                                      index[i],
                                                      ref_seq[i],
                                                      prediction_base_tensor_hp1[i],
                                                      prediction_base_tensor_hp2[i])
            batch_completed += 1

            if rank == 0 and batch_completed % 5 == 0:
                sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] " +
                                 "INFO: BATCHES PROCESSED " + str(batch_completed) + "/" + str(total_batches) + ".\n")
                sys.stderr.flush()


def predict_distributed_gpu(filepath, file_chunks, output_filepath, model_path, batch_size, total_callers, threads_per_caller, device_ids, num_workers):
    """
    Create a prediction table/dictionary of an images set using a trained model.
    :param filepath: Path to image files to predict on
    :param file_chunks: Path to chunked files
    :param batch_size: Batch size used for prediction
    :param model_path: Path to a trained model
    :param output_filepath: Path to output directory
    :param total_callers: Number of callers to spawn
    :param threads_per_caller: Number of threads to use per caller
    :param num_workers: Number of workers to be used by the dataloader
    :return: Prediction dictionary
    """
    # load the model and create an ONNX session
    transducer_model, hidden_size, gru_layers, prev_ite = \
        ModelHandler.load_simple_model_for_training(model_path,
                                                    input_channels=ImageSizeOptions.IMAGE_CHANNELS,
                                                    image_features=ImageSizeOptions.IMAGE_HEIGHT,
                                                    seq_len=ImageSizeOptions.SEQ_LENGTH,
                                                    num_classes=ImageSizeOptions.TOTAL_LABELS)
    transducer_model.eval()

    sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] INFO: MODEL LOADING TO ONNX\n")
    x = torch.zeros(1, TrainOptions.TRAIN_WINDOW, ImageSizeOptions.IMAGE_HEIGHT)
    h = torch.zeros(1, 2 * TrainOptions.GRU_LAYERS, TrainOptions.HIDDEN_SIZE)

    if not os.path.isfile(model_path + ".onnx"):
        sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] INFO: SAVING MODEL TO ONNX\n")
        torch.onnx.export(transducer_model, (x, h),
                          model_path + ".onnx",
                          training=False,
                          opset_version=10,
                          do_constant_folding=True,
                          input_names=['input_image', 'input_hidden'],
                          output_names=['output_pred', 'output_hidden'],
                          dynamic_axes={'input_image': {0: 'batch_size'},
                                        'input_hidden': {0: 'batch_size'},
                                        'output_pred': {0: 'batch_size'},
                                        'output_hidden': {0: 'batch_size'}})

    start_time = time.time()
    with concurrent.futures.ProcessPoolExecutor(max_workers=total_callers) as executor:
        futures = [executor.submit(predict, filepath, file_chunks[thread_id], output_filepath, model_path, batch_size, num_workers, threads_per_caller, device_ids[thread_id], thread_id)
                   for thread_id in range(0, total_callers)]

        for fut in concurrent.futures.as_completed(futures):
            if fut.exception() is None:
                # get the results
                thread_id = fut.result()
                sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] INFO: THREAD "
                                 + str(thread_id) + " FINISHED SUCCESSFULLY.\n")
            else:
                sys.stderr.write("ERROR: " + str(fut.exception()) + "\n")
            fut._result = None  # python issue 27144

    end_time = time.time()
    mins = int((end_time - start_time) / 60)
    secs = int((end_time - start_time)) % 60
    sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] INFO: FINISHED PREDICTION\n")
    sys.stderr.write("[" + str(datetime.now().strftime('%m-%d-%Y %H:%M:%S')) + "] INFO: ELAPSED TIME: " + str(mins) + " Min " + str(secs) + " Sec\n")