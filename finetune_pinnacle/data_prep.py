from collections import Counter
from typing import Dict, List
import numpy as np
import pandas as pd

import json, matplotlib, os, requests, time, re, mygene, zlib
from requests.adapters import HTTPAdapter, Retry
from urllib import parse, request
from urllib.parse import urlparse, parse_qs, urlencode
from xml.etree import ElementTree

import torch
from sklearn.model_selection import StratifiedGroupKFold
import matplotlib.pyplot as plt
import seaborn as sns
matplotlib.use('Agg')

LEGACY_UNIPROT_API_URL = 'https://legacy.uniprot.org/uploadlists/'
UNIPROT_API_URL = 'https://rest.uniprot.org/idmapping/'
OT_URL = "https://api.platform.opentargets.org/api/v4/graphql"
TOTAL_MAX = 20000
QUERY_BATCH_SIZE = 2048
POLLING_INTERVAL = 3
MAX_RETRY = 10  # To mitigate the effect of random state, we will redo data splitting for MAX_RETRY times if the number of positive samples in test set is less than TEST_CELLTYPE_POS_NUM_MIN
TEST_CELLTYPE_POS_NUM_MIN = 5 # For each cell type, the number of positive samples in test set must be greater than 5, or else the disease won't be evlauated


def load_PPI_data(celltype_ppi_f):

    import networkx as nx
    global_f = "/n/data1/hms/dbmi/zitnik/lab/datasets/2020-12-PPI/processed/ppi_edgelist.txt"

    def load_celltype_ppi(f):
        ppi_layers = dict()
        with open(f) as fin:
            for lin in fin:
                cluster = lin.split("\t")[1]
                ppi_layers[cluster] = lin.strip().split("\t")[2].split(",")
        print(ppi_layers.keys())
        return ppi_layers


    def load_global_PPI(f):
        G = nx.read_edgelist(f)
        print("Number of nodes:", len(G.nodes))
        print("Number of edges:", len(G.edges))
        return G

    ppi_layers = load_celltype_ppi(celltype_ppi_f)
    ppi_layers['global'] = list(load_global_PPI(global_f).nodes())
    return ppi_layers


def read_tissue_metadata(f, annotation):
    tissue_metadata = pd.read_csv(f, sep ="\t")
    print(tissue_metadata)

    celltype2compartment = dict()
    for c in tissue_metadata[annotation].unique():
        c_compartment = tissue_metadata[tissue_metadata[annotation] == c]["compartment"].unique()
        if c in celltype2compartment: 
            assert celltype2compartment[c] == c_compartment.tolist(), c
        celltype2compartment[c] = c_compartment.tolist()

    return celltype2compartment, tissue_metadata["compartment"].unique()


def process_and_split_data(embed, disease_positive_proteins, disease_negative_proteins, subtype_protein_dict, celltype_subtype, subtype_dict, disease, data_split_path, random_state, test_size):
    """
    First generate data (averaging same protein embeddings in different cell subtypes within cell types) for the particular disease.  Then split the data into train/test sets while grouping by protein and stratified by cell types.
    """
    pos_embed = []
    neg_embed = []
    pos_prots_strata = []  # Celltypes (needs to be stratified)
    neg_prots_strata = []
    pos_prots_group = []  # Protein (needs to be grouped and stay in the same data split)
    neg_prots_group = []

    # Generate data for split
    for celltype in celltype_subtype:
        if celltype in ["global", "esm"]:
            continue
        pos_prots = disease_positive_proteins[disease][celltype]
        neg_prots = disease_negative_proteins[disease][celltype]

        pos_indices = np.where(np.isin(np.array(subtype_protein_dict[celltype]), np.unique(pos_prots)))[0]
        if len(pos_indices) == 0: continue
        assert len(pos_indices) == len(pos_prots)
        print(embed[subtype_dict[celltype]].shape)
        pos_embed.append(embed[subtype_dict[celltype]][pos_indices])
        pos_prots_strata.extend([celltype] * len(pos_indices))
        pos_prots_group.extend(pos_prots)

        neg_indices = np.where(np.isin(np.array(subtype_protein_dict[celltype]), np.unique(neg_prots)))[0]
        assert len(neg_indices) != 0
        assert len(neg_indices) == len(neg_prots)
        neg_embed.append(embed[subtype_dict[celltype]][neg_indices])
        neg_prots_strata.extend([celltype] * len(neg_indices))
        neg_prots_group.extend(neg_prots)

    pos_embed = torch.tensor(np.concatenate(pos_embed, axis = 0))
    neg_embed = torch.tensor(np.concatenate(neg_embed, axis = 0))
    assert len(pos_embed) == len(pos_prots_group)
    assert len(pos_prots_group) == len(pos_prots_strata)
    
    # Conduct train-test split in a grouped and stratified way, and also ensuring positive fraction by stratifying the positive and negative embeddings separately.
    print("Checking for...", data_split_path)
    if os.path.exists(data_split_path): # Generate new splits
        print("Data split file found. Loading data splits...")
        indices = json.load(open(data_split_path, "r"))
        pos_train_indices = torch.tensor(indices["pos_train_indices"])
        pos_test_indices = torch.tensor(indices["pos_test_indices"])
        neg_train_indices = torch.tensor(indices["neg_train_indices"])
        neg_test_indices = torch.tensor(indices["neg_test_indices"])
        y_test = {celltype: np.concatenate([[1 for ind in pos_test_indices if pos_prots_strata[ind] == celltype], [0 for ind in neg_test_indices if neg_prots_strata[ind] == celltype]]) for celltype in celltype_subtype if (celltype != "global" and celltype != "esm")}
        print("Finished loading data splits.")

    else:
        print("Data split file not found. Generating data splits...")
        n_split = int(1/test_size)
        print("Number of splits...", n_split)
        def get_splits(cv):
            
            # Try all possible splits for positive examples
            for i, (pos_train_indices, pos_test_indices) in enumerate(cv.split(X=np.arange(len(pos_embed)), groups=pos_prots_group, y=pos_prots_strata)):
                y_test = {celltype: [1 for ind in pos_test_indices if pos_prots_strata[ind] == celltype] for celltype in celltype_subtype if (celltype != "global" and celltype != "esm")}
                if np.all(np.array(list(map(sum, y_test.values()))) > TEST_CELLTYPE_POS_NUM_MIN):
                    break
            
            # Randomly select train/test split for negative examples
            neg_train_indices, neg_test_indices = list(iter(cv.split(X=np.arange(len(neg_embed)), groups=neg_prots_group, y=neg_prots_strata)))[np.random.randint(0, n_split)]
            
            # Ensure that the test set has no overlap with train set
            assert np.all([Counter(pos_prots_group)[prot] == num for prot, num in Counter(np.array(pos_prots_group)[pos_test_indices]).items()])
            assert np.all([Counter(pos_prots_group)[prot] == num for prot, num in Counter(np.array(pos_prots_group)[pos_train_indices]).items()])
            
            # Combine test data
            y_test = {celltype: np.concatenate([[1 for ind in pos_test_indices if pos_prots_strata[ind] == celltype], [0 for ind in neg_test_indices if neg_prots_strata[ind] == celltype]]) for celltype in celltype_subtype if (celltype != "global" and celltype != "esm")}
            return torch.tensor(pos_train_indices), torch.tensor(pos_test_indices), torch.tensor(neg_train_indices), torch.tensor(neg_test_indices), y_test
        
        try:
            cv = StratifiedGroupKFold(n_splits=n_split, random_state=random_state, shuffle=True)  # borrow this CV generator to generate one split of what we want
            pos_train_indices, pos_test_indices, neg_train_indices, neg_test_indices, y_test = get_splits(cv)
            print("First-try successful,", Counter(np.array(pos_prots_strata)[pos_train_indices]), Counter(np.array(pos_prots_strata)[pos_test_indices]))
        
        except:  # If failed to generate splits that contain valid number of pos/neg samples under our designated random_state, try with different random state for a few times more
            count = flag = 0
            while (count < MAX_RETRY and flag == 0):
                try:
                    new_random_state = np.random.randint(0, 100000)
                    cv = StratifiedGroupKFold(n_splits=n_split, random_state=new_random_state, shuffle=True)  # borrow this CV generator to generate one split of what we want
                    pos_train_indices, pos_test_indices, neg_train_indices, neg_test_indices, y_test = get_splits(cv)
                    print(("Re-tried successfully with new seed %s," % str(new_random_state)), Counter(np.array(pos_prots_strata)[pos_train_indices]), Counter(np.array(pos_prots_strata)[pos_test_indices]))
                    flag = 1
                except:
                    count += 1
                    continue

            if flag == 0:
                raise ValueError(f"Could not generate a valid train-test split for disease {disease}. Number of positive test samples in some cell types is lower than {TEST_CELLTYPE_POS_NUM_MIN}.")

        indices_dict = {"pos_train_indices": pos_train_indices.tolist(),
                        "pos_test_indices": pos_test_indices.tolist(),
                        "neg_train_indices": neg_train_indices.tolist(),
                        "neg_test_indices": neg_test_indices.tolist()}
        print("Saving data splits to file", data_split_path)
        with open(data_split_path, "w") as outfile:
            json.dump(indices_dict, outfile)
        print("Finished saving data splits.")
    
    X_train = torch.cat([pos_embed[pos_train_indices], neg_embed[neg_train_indices]], dim = 0)
    groups_train = [pos_prots_group[ind] for ind in pos_train_indices] + [neg_prots_group[ind] for ind in neg_train_indices]
    groups_train_pos = [pos_prots_group[ind] for ind in pos_train_indices]
    groups_train_neg = [neg_prots_group[ind] for ind in neg_train_indices]
    cts_train = [pos_prots_strata[ind] for ind in pos_train_indices] + [neg_prots_strata[ind] for ind in neg_train_indices]
    y_train = np.concatenate([np.ones(len(pos_train_indices)), np.zeros(len(neg_train_indices))])
    
    X_test = dict()
    groups_test = dict()
    groups_test_pos = []
    groups_test_neg = []
    for cat in celltype_subtype:
        if cat == "global" or cat == "esm": continue
        pos_cat_embs = [pos_embed[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat]
        neg_cat_embs = [neg_embed[ind] for ind in neg_test_indices if neg_prots_strata[ind] == cat]
        
        if len(pos_cat_embs) > 0 and len(neg_cat_embs) > 0:
            X_test[cat] = torch.cat([torch.stack(pos_cat_embs), torch.stack(neg_cat_embs)])
            groups_test[cat] = [pos_prots_group[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat] + [neg_prots_group[ind] for ind in neg_test_indices if neg_prots_strata[ind] == cat]
            groups_test_pos.extend([pos_prots_group[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat])
            groups_test_neg.extend([neg_prots_group[ind] for ind in neg_test_indices if neg_prots_strata[ind] == cat])
            
            assert len(set(groups_test[cat]).intersection(set(groups_train))) == 0, set(groups_test[cat]).intersection(set(groups_train))

        elif len(pos_cat_embs) == 0 and len(neg_cat_embs) > 0:
            print("Cell type has only negative examples:", cat)
            #X_test[cat] = torch.stack(neg_cat_embs)
            assert len([pos_prots_group[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat]) == 0
            #groups_test[cat] = [neg_prots_group[ind] for ind in neg_test_indices if neg_prots_strata[ind] == cat]
        elif len(pos_cat_embs) > 0 and len(neg_cat_embs) == 0:
            print("Cell type has only positive examples:", cat)
            X_test[cat] = torch.stack(pos_cat_embs)
            assert len([neg_prots_group[ind] for ind in neg_test_indices if neg_prots_strata[ind] == cat]) == 0
            groups_test[cat] = [pos_prots_group[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat]
            groups_test_pos.extend([pos_prots_group[ind] for ind in pos_test_indices if pos_prots_strata[ind] == cat])
            
            assert len(set(groups_test[cat]).intersection(set(groups_train))) == 0
        else:
            print("Cell type has no positive or negative examples:", cat)

    for k, v in groups_test.items():
        #print(k, set(v).intersection(set(groups_train)))
        assert len(set(v).intersection(set(groups_train))) == 0, (k, set(v).intersection(set(groups_train)))

    data_split_names_path = data_split_path.split(".json")[0] + "_name.json"
    print(data_split_names_path)
    if not os.path.exists(data_split_names_path): # Generate new splits
        indices_name_dict = {"pos_train_names": list(set(groups_train_pos)),
                             "pos_test_names": list(set(groups_test_pos)),
                             "neg_train_names": list(set(groups_train_neg)),
                             "neg_test_names": list(set(groups_test_neg))}
        for k1, v1 in indices_name_dict.items():
            for k2, v2 in indices_name_dict.items():
                if k1 == k2: continue
                assert len(set(v1).intersection(set(v2))) == 0, (k1, k2)

        with open(data_split_names_path, "w") as outfile:
            json.dump(indices_name_dict, outfile)

    return X_train, X_test, y_train, y_test, groups_train, cts_train, groups_test


def get_disease_descendants(disease: str, source: str = 'ot', curated_disease_dir: str = None):
    """
    Get all descendants of a disease.
    """
    if source == 'ot':
        # Get all descendants of disease from OT
        flag = 0
        for fn in os.listdir(curated_disease_dir):
            with open(curated_disease_dir + fn) as f:
                diseases = f.readlines()
                for dis in diseases:
                    dis = json.loads(dis)
                    if dis['id'] == disease:
                        flag = 1
                        try:
                            all_disease = dis['descendants'] + [disease]
                            print(f'{disease} has {len(all_disease)-1} descendants')
                        except:
                            print(f'found {disease} has no descendants')
                            all_disease = [disease]
                            break
        assert flag == 1, f'{disease} not found in current database!'
    
    elif source == 'efo':
        # Get all descendants of that disease directly from EFO

        if disease.split('_')[0] == 'MONDO':
            efo_hierdesc = 'https://www.ebi.ac.uk/ols/api/ontologies/efo/terms/http%253A%252F%252Fpurl.obolibrary.org%252Fobo%252F' + disease + '/hierarchicalDescendants?size=5000'
        elif disease.split('_')[0] == 'EFO':
            efo_hierdesc = 'https://www.ebi.ac.uk/ols/api/ontologies/efo/terms/http%253A%252F%252Fwww.ebi.ac.uk%252Fefo%252F' + disease + '/hierarchicalDescendants?size=5000'
        elif disease.split('_')[0] == 'Orphanet':
            efo_hierdesc = 'https://www.ebi.ac.uk/ols/api/ontologies/orphanet/terms/http%253A%252F%252Fwww.orpha.net%252FORDO%252F' + disease + '/hierarchicalDescendants?size=5000'
        else:
            raise NotImplementedError

        disease_descendants = requests.request('GET', efo_hierdesc)
        assert disease_descendants.status_code==200

        # First, read the disease files and curate all diseases in this therapeutic area.
        raw_disease = json.loads(disease_descendants.text)
        assert raw_disease['page']['totalPages']==1

        all_disease = [disease]
        for raw in raw_disease['_embedded']['terms']:
            all_disease.append(raw['short_form'])
            assert raw['short_form'].split('_') == raw['obo_id'].split(':')
            try:
                for id in raw['annotation']['database_cross_reference']:
                    all_disease.append(id.replace(':', '_'))
            except:
                pass
            
        all_disease = set(all_disease)
    
    return all_disease


def get_all_drug_evidence(evidence_files: List, evidence_dir: str, all_disease: List, chembl2db: dict):
    """
    Get all target-disease associations with clinically relevant evidence, i.e. mediated by approved drugs / clinical candidate >= II (must be 'Completed' if II)
    """
    all_evidence = []
    for file in evidence_files:
        evidence_file = evidence_dir + file
        with open(evidence_file) as f:
            raw_evidence = f.readlines()
            evidence_list = [json.loads(evidence) for evidence in raw_evidence]

        for evidence in evidence_list:
            if ('diseaseFromSourceMappedId' in evidence.keys()) and ('clinicalPhase' in evidence.keys()) and (evidence['diseaseFromSourceMappedId'] in all_disease) and ((evidence['clinicalPhase']>=3) or (evidence['clinicalPhase']==2 and 'clinicalStatus' in evidence.keys() and evidence['clinicalStatus']=='Completed')):
                if 'clinicalStatus' in evidence.keys():
                    try:
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], evidence['clinicalStatus'], chembl2db[evidence['drugId']]])
                    except:
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], evidence['clinicalStatus'], evidence['drugId']])
                else:
                    try:
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], np.nan, chembl2db[evidence['drugId']]])
                    except: 
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], np.nan, evidence['drugId']])
                        
            elif ('diseaseId' in evidence.keys()) and ('clinicalPhase' in evidence.keys()) and (evidence['diseaseId'] in all_disease) and ((evidence['clinicalPhase']>=3) or (evidence['clinicalPhase']==2 and 'clinicalStatus' in evidence.keys() and evidence['clinicalStatus']=='Completed')):
                if 'clinicalStatus' in evidence.keys():
                    try:    
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], evidence['clinicalStatus'], chembl2db[evidence['drugId']]])
                    except:
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], evidence['clinicalStatus'], evidence['drugId']])
                else:
                    try:
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], np.nan, chembl2db[evidence['drugId']]])
                    except: 
                        all_evidence.append([evidence['diseaseFromSourceMappedId'], evidence['diseaseId'], evidence['targetId'], evidence['targetFromSourceId'], evidence['clinicalPhase'], np.nan, evidence['drugId']])
            
    drug_evidence_data = pd.DataFrame(all_evidence, columns=['diseaseFromSourceMappedId', 'diseaseId', 'targetId', 'targetFromSourceId', 'clinicalPhase', 'clinicalStatus', 'drugId']).sort_values(by='targetId')  # actually, it's drug-mediated target-disease association evidence data
    assert drug_evidence_data.diseaseFromSourceMappedId.isin(all_disease).all()
    assert drug_evidence_data.clinicalPhase.isin([2,3,4]).all()

    return drug_evidence_data


def get_all_associated_targets(disease: str):
    """
    Get all target-disease associations, except for those with only text mining (literature) evidence.
    """
    # Get all kinds of valid drug-disease associations
    def try_get_targets(index: int, size: int, all_targets: list, disease_id: str, query_string: str):
        """
        Try get targets for a disease from the API for the region of indices that contains the stale index.
        """
        if size!=1:
            index_temp = index * 2
            size_temp = size // 2
            for idx in [index_temp, index_temp+1]:
                variables = {"efoId":disease_id, "index":idx, 'size':size_temp}
                r = requests.post(OT_URL, json={"query": query_string, "variables": variables})
                assert r.status_code == 200
                try:
                    api_response = json.loads(r.text)
                    if type(api_response['data']['disease']['associatedTargets']['rows'])==list:
                        all_targets.extend(api_response['data']['disease']['associatedTargets']['rows'])
                    else:
                        all_targets.append(api_response['data']['disease']['associatedTargets']['rows'])
                except:
                    print(f"The stale index is within {str(idx*size_temp)}~{str((idx+1)*size_temp-1)}")
                    try_get_targets(idx, size_temp, all_targets, disease_id, query_string)
        else:
            print(f"Found stale index at index: {str(index)}!")
        
        return

    query_string = """
        query disease($efoId: String!, $index: Int!, $size: Int!) {
        disease(efoId: $efoId){
            id
            name
            associatedTargets(page: { index: $index, size: $size }) {
            rows {
                score
                datatypeScores{
                    id
                    score
                }
                target {
                    id
                    approvedSymbol
                }
            }
            }
        }
        }
    """

    all_targets = []
    for index in range(TOTAL_MAX//QUERY_BATCH_SIZE + 1):
        # Set variables object of arguments to be passed to endpoint
        variables = {"efoId":disease, "index":index, 'size':QUERY_BATCH_SIZE}

        # Perform POST request and check status code of response
        r = requests.post(OT_URL, json={"query": query_string, "variables": variables})
        assert r.status_code == 200

        # Transform API response from JSON into Python dictionary and print in console
        try:
            api_response = json.loads(r.text)
            all_targets.extend(api_response['data']['disease']['associatedTargets']['rows'])
        except:
            print(f'Unknown error when quering OT for {disease}.  Attemtping to get around the stale record...')
            try_get_targets(index, QUERY_BATCH_SIZE, all_targets, disease, query_string)

    all_associated_targets = [tar['target']['approvedSymbol'] for tar in all_targets if (len(tar['datatypeScores'])>1 or tar['datatypeScores'][0]['id']!='literature')]  #  All proteins associated with the disease, excluding those with only text mining evidence
    ensg2otgenename = {tar['target']['id']:tar['target']['approvedSymbol'] for tar in all_targets}

    print(f'Found {len(all_associated_targets)} associated targets for {disease}.')

    return all_associated_targets, ensg2otgenename


def evidence2genename(drug_evidence_data: pd.DataFrame, ensg2otgenename: dict):
    """
    Convert ENSG id and UniProt id in evidence to gene name through three ways combined, i.e. get all targets with clinically relevant evidence before intersecting with cell type PPIs
    """
    # UniProt
    try:
        uniprot_list = ' '.join(drug_evidence_data.targetFromSourceId.unique())
        params = {
            'from': 'ACC+ID',
            'to': 'GENENAME',
            'format': 'tab',
            'query': uniprot_list
        }

        data = parse.urlencode(params)
        data = data.encode('utf-8')
        req = request.Request(LEGACY_UNIPROT_API_URL, data)
        with request.urlopen(req) as f:
            response = f.read()
        res = response.decode('utf-8')
        uniprot2name = {ins.split('\t')[0]:ins.split('\t')[1] for ins in res.split('\n')[1:-1]}

    except:
        # Adapted from https://www.uniprot.org/help/id_mapping
        retries = Retry(total=5, backoff_factor=0.25, status_forcelist=[500, 502, 503, 504])
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=retries))

        def submit_job(src, dst, ids):
            """
            Submit job to UniProt ID mapping server, where `ids` is a str of identifiers separated by ','.
            """
            r = requests.post(
                f"{UNIPROT_API_URL}/run", 
                data={"from": src, "to": dst, "ids": ids},
            )
            r.raise_for_status()
            return r.json()["jobId"]

        def get_next_link(headers):
            re_next_link = re.compile(r'<(.+)>; rel="next"')
            if "Link" in headers:
                match = re_next_link.match(headers["Link"])
                if match:
                    return match.group(1)

        def check_id_mapping_results_ready(job_id):
            while True:
                r = session.get(f"{UNIPROT_API_URL}/status/{job_id}")
                r.raise_for_status()
                job = r.json()
                if "jobStatus" in job:
                    if job["jobStatus"] == "RUNNING":
                        print(f"Retrying in {POLLING_INTERVAL}s")
                        time.sleep(POLLING_INTERVAL)
                    else:
                        raise Exception(job["jobStatus"])
                else:
                    return bool(job["results"] or job["failedIds"])

        def get_batch(batch_response, file_format, compressed):
            batch_url = get_next_link(batch_response.headers)
            while batch_url:
                batch_response = session.get(batch_url)
                batch_response.raise_for_status()
                yield decode_results(batch_response, file_format, compressed)
                batch_url = get_next_link(batch_response.headers)

        def combine_batches(all_results, batch_results, file_format):
            if file_format == "json":
                for key in ("results", "failedIds"):
                    if key in batch_results and batch_results[key]:
                        all_results[key] += batch_results[key]
            elif file_format == "tsv":
                return all_results + batch_results[1:]
            else:
                return all_results + batch_results
            return all_results

        def get_id_mapping_results_link(job_id):
            url = f"{UNIPROT_API_URL}/details/{job_id}"
            request = session.get(url)
            request.raise_for_status()
            return request.json()["redirectURL"]

        def decode_results(response, file_format, compressed):
            if compressed:
                decompressed = zlib.decompress(response.content, 16 + zlib.MAX_WBITS)
                if file_format == "json":
                    j = json.loads(decompressed.decode("utf-8"))
                    return j
                elif file_format == "tsv":
                    return [line for line in decompressed.decode("utf-8").split("\n") if line]
                elif file_format == "xlsx":
                    return [decompressed]
                elif file_format == "xml":
                    return [decompressed.decode("utf-8")]
                else:
                    return decompressed.decode("utf-8")
            elif file_format == "json":
                return response.json()
            elif file_format == "tsv":
                return [line for line in response.text.split("\n") if line]
            elif file_format == "xlsx":
                return [response.content]
            elif file_format == "xml":
                return [response.text]
            return response.text

        def get_xml_namespace(element):
            m = re.match(r"\{(.*)\}", element.tag)
            return m.groups()[0] if m else ""

        def merge_xml_results(xml_results):
            merged_root = ElementTree.fromstring(xml_results[0])
            for result in xml_results[1:]:
                root = ElementTree.fromstring(result)
                for child in root.findall("{http://uniprot.org/uniprot}entry"):
                    merged_root.insert(-1, child)
            ElementTree.register_namespace("", get_xml_namespace(merged_root[0]))
            return ElementTree.tostring(merged_root, encoding="utf-8", xml_declaration=True)

        def print_progress_batches(batch_index, size, total):
            n_fetched = min((batch_index + 1) * size, total)
            print(f"Fetched evidence: {n_fetched} / {total}")

        def get_id_mapping_results_search(url):
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            file_format = query["format"][0] if "format" in query else "json"
            if "size" in query:
                size = int(query["size"][0])
            else:
                size = 500
                query["size"] = size
            compressed = (
                query["compressed"][0].lower() == "true" if "compressed" in query else False
            )
            parsed = parsed._replace(query=urlencode(query, doseq=True))
            url = parsed.geturl()
            request = session.get(url)
            request.raise_for_status()
            results = decode_results(request, file_format, compressed)
            total = int(request.headers["x-total-results"])
            print_progress_batches(0, size, total)
            for i, batch in enumerate(get_batch(request, file_format, compressed), 1):
                results = combine_batches(results, batch, file_format)
                print_progress_batches(i, size, total)
            if file_format == "xml":
                return merge_xml_results(results)
            return results

        def get_id_mapping_results_stream(url):
            if "/stream/" not in url:
                url = url.replace("/results/", "/stream/")
            request = session.get(url)
            request.raise_for_status()
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            file_format = query["format"][0] if "format" in query else "json"
            compressed = (
                query["compressed"][0].lower() == "true" if "compressed" in query else False
            )
            return decode_results(request, file_format, compressed)
        
        job_id = submit_job(
            src="UniProtKB_AC-ID", 
            dst="Gene_Name", 
            ids=drug_evidence_data.targetFromSourceId.unique().tolist()
        )

        if check_id_mapping_results_ready(job_id):
            link = get_id_mapping_results_link(job_id)
            results = get_id_mapping_results_search(link)

        uniprot2name = {rec['from']:rec['to'] for rec in results['results']}

    # ENSG --> gene name through mygene
    mg = mygene.MyGeneInfo()
    out = mg.querymany(drug_evidence_data.targetId.unique())
    ensg2name = {}
    for o in out:
        ensg2name[o['query']] = o['symbol']

    # Not sure why these didn't get added
    if "ENSG00000187733" not in ensg2otgenename: ensg2otgenename["ENSG00000187733"] = "AMY1C"

    disease_drug_targets = set(uniprot2name.values())
    disease_drug_targets.update(ensg2name.values())

    # ENSG --> gene name through OT
    disease_drug_targets.update([ensg2otgenename[ensg] for ensg in drug_evidence_data.targetId])

    print(f'Found {len(disease_drug_targets)} targets with clinically relevant evidence.')
    
    return disease_drug_targets
    

def get_labels_from_evidence(celltype_protein_dict: Dict[str, List[str]], therapeutic_areas: list, evidence_dir: str, all_drug_targets_path: str, curated_disease_dir: str, chembl2db_path: str, 
                             positive_protein_prefix: str, negative_protein_prefix: str, raw_targets_prefix: str, 
                             overwrite: bool, disease_drug_evidence_prefix = "", wandb = None):
    """
    Get positive and negative targets associated with each disease and descendants.
    """
    
    # Read in CHEMBL data
    chembl_db_df = pd.read_table(chembl2db_path) 
    chembl_db_df.columns = ['chembl', 'db']
    chembl2db = chembl_db_df.set_index('chembl').to_dict()['db']

    # Read in all approved drug-target data
    dti_tab = pd.read_csv(all_drug_targets_path, index_col=0)  # approved drug-target table
    assert dti_tab['Drug IDs'].isna().sum()==0
    dti_tab = dti_tab[dti_tab.Species=='Humans']
    druggable_targets = dti_tab[['Gene Name', 'GenAtlas ID']]
    druggable_targets = set(druggable_targets.values.flatten())
    druggable_targets.remove(np.nan)  # all approved drugs' targets

    positive_proteins = {}
    negative_proteins = {}
    clinically_relevant_targets = {}
    evidence_files = os.listdir(evidence_dir)

    for disease in therapeutic_areas:
        if not overwrite:
            try:
                with open(positive_protein_prefix + disease + '.json', 'r') as f:
                    temp = json.load(f)
                    positive_proteins.update(temp)
                with open(negative_protein_prefix + disease + '.json', 'r') as f:
                    temp = json.load(f)
                    negative_proteins.update(temp)
                with open(raw_targets_prefix + disease + '.json', 'r') as f:
                    temp = json.load(f)
                    clinically_relevant_targets.update(temp)

                if "esm" not in positive_proteins[disease]:
                    positive_proteins[disease]['esm'] = positive_proteins[disease]['global']  # esm positive proteins are the union of all celltype positive proteins
                    negative_proteins[disease]['esm'] = negative_proteins[disease]['global']  # esm negative proteins are the union of all celltype negative proteins
                
                continue
            except:
                pass

        # Get all disease descendants (we include indirect evidence)
        all_disease = get_disease_descendants(disease, source='ot', curated_disease_dir=curated_disease_dir)
        if wandb is not None:
            wandb.log({f'number of disease descendants':len(all_disease)})
        
        # Look for clinically relevant evidence on targets related to each of the diseases.
        disease_drug_evidence_data = get_all_drug_evidence(evidence_files, evidence_dir, all_disease, chembl2db)

        # Get all associated targets of disease
        all_associated_targets, ensg2otgenename = get_all_associated_targets(disease)

        # Convert clinically relevant targets to gene names
        disease_drug_targets = evidence2genename(disease_drug_evidence_data, ensg2otgenename)
        
        # Saving disease/drug-target evidence
        if disease_drug_evidence_prefix != "":
            disease_drug_evidence_data["targetId_genename"] = disease_drug_evidence_data["targetId"].map(ensg2otgenename)
            print(disease_drug_evidence_data)
            print(Counter("Phase " + str(a) + "," + str(b) for a, b in zip(disease_drug_evidence_data["clinicalPhase"].tolist(), disease_drug_evidence_data["clinicalStatus"].tolist())))
            print(disease_drug_evidence_data["drugId"].unique())
            disease_drug_evidence_data.to_csv(disease_drug_evidence_prefix + disease + ".csv", index = False, sep = "\t")

        # Get positive and negative labels for proteins.  Note that globals are included in cell_type_protein_dict's keys, but it can also directly be built from the union of all other positive/negatives.
        positive_proteins[disease] = {ct: list(disease_drug_targets.intersection(ppi_proteins)) for ct, ppi_proteins in celltype_protein_dict.items() if (ct != 'global' and ct != 'esm')}  # PPI proteins associated with the disease with drug or clinical candidate > II's evidence
        negative_proteins[disease] = {ct: list(set(ppi_proteins).difference(all_associated_targets).intersection(druggable_targets)) for ct, ppi_proteins in celltype_protein_dict.items() if (ct != 'global' and ct != 'esm')}  # PPI proteins that are not associated with the disease except for text mining, but are still druggable
        positive_proteins[disease]['global'] = np.unique(sum([prots for prots in positive_proteins[disease].values()], start=[])).tolist()  # global positive proteins are the union of all celltype positive proteins
        negative_proteins[disease]['global'] = np.unique(sum([prots for prots in negative_proteins[disease].values()], start=[])).tolist()  # global negative proteins are the union of all celltype negative proteins
        positive_proteins[disease]['esm'] = positive_proteins[disease]['global']  # esm positive proteins are the union of all celltype positive proteins
        negative_proteins[disease]['esm'] = negative_proteins[disease]['global']  # esm negative proteins are the union of all celltype negative proteins

        # Collect all targets (for diseases, not considering the intersection with PPI).
        clinically_relevant_targets[disease] = list(disease_drug_targets)

        with open(positive_protein_prefix + disease + '.json', 'w') as f:
            json.dump({disease: positive_proteins[disease]}, f)
        with open(negative_protein_prefix + disease + '.json', 'w') as f:
            json.dump({disease: negative_proteins[disease]}, f)
        with open(raw_targets_prefix + disease + '.json', 'w') as f:
            json.dump({disease: clinically_relevant_targets[disease]}, f)
    
    positive_protein_counts_celltype = pd.DataFrame(positive_proteins).rename(index={ind:ind[:-2] for ind in positive_proteins[disease].keys() if (ind != 'global' and ind != 'esm')}).reset_index().melt(id_vars = ['index']).groupby(by=['index', 'variable']).aggregate(list).applymap(lambda x: len(np.unique(sum(x, start = [])))).reset_index()

    # plt.figure(figsize=(8, 4))
    sns.barplot(x='variable', y='value', data=positive_protein_counts_celltype, hue='index')
    plt.legend(bbox_to_anchor=(-0.45, 1), loc='upper left', ncol=1, fontsize=8)
    plt.xticks(rotation=30, ha='right', rotation_mode='anchor')
    plt.xlabel('')
    plt.ylabel('# of positive samples per cell subtype')
    plt.savefig(positive_protein_prefix + disease + '.png', bbox_inches = "tight")
    if wandb is not None:
        wandb.log({f'Number of all positive samples':plt})

    return positive_proteins, negative_proteins, clinically_relevant_targets


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--celltype_ppi", type=str, help="Filename (prefix) of cell type PPI.")
    parser.add_argument('--disease', type=str, default='MONDO_0000569')
    parser.add_argument('--evidence_dir', type=str, default="/n/data1/hms/dbmi/zitnik/lab/datasets/2022-06-Open_Targets/data/evidence/sourceId=chembl/")
    parser.add_argument('--all_drug_targets_path', type=str, default="/home/yeh803/workspace/scDrug/pipeline/AWARE/targets/all_approved.csv")
    parser.add_argument('--curated_disease_dir', type=str, default="/n/data1/hms/dbmi/zitnik/lab/datasets/2022-06-Open_Targets/data/diseases/")
    parser.add_argument('--chembl2db_path', type=str, default="/n/data1/hms/dbmi/zitnik/lab/datasets/2022-06-Open_Targets/data/chembl2db.txt")  # Download mapping from ChEMBL id to DrugBank id from https://ftp.ebi.ac.uk/pub/databases/chembl/UniChem/data/wholeSourceMapping/src_id1/src1src2.txt (version: 13-Apr-2022)
    parser.add_argument('--disease_drug_evidence_prefix', type=str, default="/home/yeh803/workspace/scDrug/pipeline/AWARE/targets/disease_drug_evidence_")
    parser.add_argument('--positive_proteins_prefix', type=str, default="/home/yeh803/workspace/scDrug/pipeline/AWARE/targets/positive_proteins_")
    parser.add_argument('--negative_proteins_prefix', type=str, default="/home/yeh803/workspace/scDrug/pipeline/AWARE/targets/negative_proteins_")
    parser.add_argument('--raw_targets_prefix', type=str, default="/home/yeh803/workspace/scDrug/pipeline/AWARE/targets/raw_targets_")
    args = parser.parse_args()

    therapeutic_areas = args.disease.split(",")
    print(therapeutic_areas)
    subtype_protein_dict = load_PPI_data(args.celltype_ppi)
    celltype2compartment, compartments = read_tissue_metadata("/n/data1/hms/dbmi/zitnik/lab/datasets/2022-09-TabulaSapiens/processed/ts_data_tissue.csv", "cell_ontology_class")

    disease_positive_proteins, disease_negative_proteins, clinically_relevant_targets = get_labels_from_evidence(subtype_protein_dict, therapeutic_areas, args.evidence_dir, args.all_drug_targets_path, args.curated_disease_dir, args.chembl2db_path, args.positive_proteins_prefix, args.negative_proteins_prefix, args.raw_targets_prefix, overwrite = True, disease_drug_evidence_prefix = args.disease_drug_evidence_prefix)
    
    for d in clinically_relevant_targets:
        compartment_counts = dict.fromkeys(compartments)
        for c, v in disease_positive_proteins[d].items():
            if c == "global" or c == "esm": continue
            assert len(v) == len(set(v).intersection(set(clinically_relevant_targets[d])))
            
            for c_compartment in celltype2compartment[c]:
                print("Adding %s to %s (%d)" % (c, c_compartment, len(v)))
                if compartment_counts[c_compartment] == None: compartment_counts[c_compartment] = []
                compartment_counts[celltype2compartment[c][0]].append(len(v))
                
        for compartment, c_count in compartment_counts.items():
            print(compartment, "Min:", min(c_count), "Max:", max(c_count), "Average:", np.mean(c_count), "+/-", np.std(c_count))


if __name__ == '__main__':
    main()