import json
import os
import os.path as osp
import pickle
import numpy as np

def ensure_dir(path):
    if not osp.exists(path):
        os.makedirs(path)

def assert_dir(path):
    assert osp.exists(path)

def load_pkl_data(filename):
    with open(filename, 'rb') as handle:
        data_dict = pickle.load(handle)
    return data_dict

def write_pkl_data(data_dict, filename):
    with open(filename, 'wb') as handle:
        pickle.dump(data_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

def load_json(filename):
    file = open(filename)
    data = json.load(file)
    file.close()
    return data

def write_json(data_dict, filename):
    json_obj = json.dumps(data_dict, indent=4)

    with open(filename, "w") as outfile:
        outfile.write(json_obj)

def get_print_format(value):
    if isinstance(value, int):
        return 'd'
    if isinstance(value, str):
        return 's'
    if value == 0:
        return '.3f'
    if value < 1e-6:
        return '.3e'
    if value < 1e-3:
        return '.6f'
    return '.6f'

def write_to_txt(file, lines):
    with open(file, 'w') as f:
        for line in lines:
            f.write(line + '\n')


def get_format_strings(kv_pairs):
    r"""Get format string for a list of key-value pairs."""
    log_strings = []
    for key, value in kv_pairs:
        fmt = get_print_format(value)
        format_string = '{}: {:' + fmt + '}'
        log_strings.append(format_string.format(key, value))
    return log_strings

def log_softmax_to_probabilities(log_softmax, epsilon=1e-5):
    softmax = np.exp(log_softmax)
    probabilities = softmax / np.sum(softmax)
    assert np.sum(probabilities) >= 1.0 - epsilon and np.sum(probabilities) <= 1.0 + epsilon 
    return probabilities

def merge_duplets(duplets):
    merged = []
    for duplet in duplets:
        merged_duplet = None
        for i, m in enumerate(merged):
            if any(id in m for id in duplet):
                if merged_duplet is None:
                    merged_duplet = m
                else:
                    merged_duplet.extend(m)
                    merged.pop(i)
        if merged_duplet is not None:
            merged_duplet.extend(duplet)
        else:
            merged.append(list(duplet))
    
    merged_set = list()
    for merge in merged:
        merged_set.append(sorted(list(set(merge))))
    return merged_set

def update_dict(dictionary, to_add_dict):
    for key in dictionary.keys():
        if key in ['RRE', 'RTE'] and to_add_dict['recall'] > 0.0:
            dictionary[key].append(to_add_dict[key])
        else:
            dictionary[key].append(to_add_dict[key])
    
    return dictionary

def get_log_string(result_dict, name=None, epoch=None, max_epoch=None, iteration=None, max_iteration=None, lr=None, timer=None):
    log_strings = []
    if name is not None: 
        log_strings.append(name)
    if epoch is not None:
        epoch_string = f'Epoch: {epoch}'
        if max_epoch is not None:
            epoch_string += f'/{max_epoch}'
        log_strings.append(epoch_string)
    if iteration is not None:
        iter_string = f'iter: {iteration}'
        if max_iteration is not None:
            iter_string += f'/{max_iteration}'
        if epoch is None:
            iter_string = iter_string.capitalize()
        log_strings.append(iter_string)
    if 'metadata' in result_dict:
        log_strings += result_dict['metadata']
    for key, value in result_dict.items():
        if key != 'metadata':
            format_string = '{}: {:' + get_print_format(value) + '}'
            log_strings.append(format_string.format(key, value))
    if lr is not None:
        log_strings.append('lr: {:.3e}'.format(lr))
    if timer is not None:
        log_strings.append(timer.tostring())
    
    message = ', '.join(log_strings)
    return message

def name2idx(file_name):
    name2idx = {}
    index = 0
    with open(file_name) as f:
        lines = f.readlines()
        for line in lines:
            className = line.split('\n')[0]
            name2idx[className] = index
            index += 1
    
    return name2idx

def idx2name(file_name):
    idx2name = {}
    with open(file_name) as f:
        lines = f.read().splitlines()
        for line in lines:
            split_str = line.split('	')
            idx = split_str[0]
            name = split_str[-1]
            idx2name[int(idx)] = name
    return idx2name

def get_key_by_value(dictionary, value):
    for key, values in dictionary.items():
        if value in values: return key

# calculate average of a list
def ave_list(lists):
    if len(lists) == 0:
        return 0
    else:
        return sum(lists)*1.0/len(lists)
    
import os, threading, queue, subprocess
def RunBashBatch(commands, jobs_per_step = 1):
    class BashThread(threading.Thread):
        def __init__(self,task_queue, id):
            threading.Thread.__init__(self)
            self.queue = task_queue
            self.th_id = id
            self.start()

        def run(self):
            while True:
                try:
                    command = self.queue.get(block=False)
                    subprocess.call(command, shell=True)
                    self.queue.task_done()
                except queue.Empty:
                    break
    class BashThreadPool():
        def __init__(self,task_queue,thread_num):
            self.queue = task_queue
            self.pool = []
            for i in range(thread_num):
                self.pool.append(BashThread(task_queue, i))

        def joinAll(self):
            self.queue.join()
    # task submission
    commands_queue = queue.Queue()
    for command in commands:
        commands_queue.put(command)
    map_eval_thread_pool = BashThreadPool(commands_queue, jobs_per_step)
    map_eval_thread_pool.joinAll()