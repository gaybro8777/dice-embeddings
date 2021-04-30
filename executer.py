import warnings

warnings.simplefilter("ignore", UserWarning)
from util.dataset_classes import StandardDataModule, KvsAll, CVDataModule
from util.knowledge_graph import KG
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset
from sklearn.model_selection import KFold
from static_funcs import *
import numpy as np
from pytorch_lightning import loggers as pl_loggers
import pandas as pd
import json


class Execute:
    def __init__(self, args):
        args = preprocesses_input_args(args)
        sanity_checking_with_arguments(args)
        self.args = args
        self.dataset = KG(data_dir=args.path_dataset_folder, add_reciprical=args.add_reciprical)
        self.args.num_entities, self.args.num_relations = self.dataset.num_entities, self.dataset.num_relations
        self.storage_path = create_experiment_folder(folder_name=args.storage_path)
        self.logger = create_logger(name=self.args.model, p=self.storage_path)
        self.trainer = None
        self.args.default_root_dir = self.storage_path + '/checkpoints'

        self.scoring_technique = args.scoring_technique
        self.neg_ratio = args.negative_sample_ratio

        if self.args.batch_size > len(self.dataset.train_set):
            self.args.batch_size = len(self.dataset.train_set)

        if self.args.model == 'Shallom' and self.scoring_technique == 'NegSample':
            self.logger.info(
                'Shallom can not be trained with Negative Sampling. Scoring technique is changed to KvsALL')
            self.scoring_technique = 'KvsAll'

        if self.scoring_technique == 'KvsAll':
            self.neg_ratio = None

    def store(self, trained_model) -> None:
        """
        Store trained_model model and save embeddings into csv file.
        :param trained_model:
        :return:
        """
        # Save Torch model.
        torch.save(trained_model.state_dict(), self.storage_path + '/model.pt')

        with open(self.storage_path + '/configuration.json', 'w') as file_descriptor:
            temp = vars(self.args)
            temp.pop('gpus')
            temp.pop('tpu_cores')
            json.dump(temp, file_descriptor)

        if trained_model.name == 'Shallom':
            entity_emb = trained_model.get_embeddings()
        else:
            entity_emb, relation_ebm = trained_model.get_embeddings()
            try:
                pd.DataFrame(relation_ebm, index=self.dataset.relations).to_csv(
                    self.storage_path + '/' + trained_model.name + '_relation_embeddings.csv')
            except:
                print('Exception occurred at saving relation embeddings. Computation will continue')

        try:
            pd.DataFrame(entity_emb, index=self.dataset.entities).to_csv(
                self.storage_path + '/' + trained_model.name + '_entity_embeddings.csv')
        except:
            print('Exception occurred at saving entity embeddings.Computation will continue')

    def start(self) -> None:
        # 1. Train and Evaluate
        trained_model = self.train_and_eval()
        # 2. Store trained model
        self.store(trained_model)

    def train_and_eval(self):
        """
        Training procedure

        1. KvsAll

        2. OnlyTrain

        3. K fold cross validation
        :return:
        """
        self.logger.info('--- Parameters are parsed for training ---')
        self.trainer = pl.Trainer.from_argparse_args(self.args)
        # 1. Check validation and test datasets are available.
        if self.dataset.is_valid_test_available():
            if self.scoring_technique == 'NegSample':
                trained_model = self.training_negative_sampling()
            elif self.scoring_technique == 'KvsAll':
                # KvsAll or negative sampling
                trained_model = self.training()
            else:
                raise ValueError(f'Invalid argument: {self.scoring_technique}')
        else:
            self.logger.info(f'There is no validation and test sets available.')
            if self.args.num_folds_for_cv < 2:
                self.logger.info(
                    f'No test set is found and k-fold cross-validation is set to less than 2 (***num_folds_for_cv*** => {self.args.num_folds_for_cv}). Hence we do not evaluate the model')
                trained_model = self.training()
            else:
                trained_model = self.k_fold_cross_validation()
        self.logger.info('--- Training is completed  ---')
        return trained_model

    def get_batch_1_to_N(self, er_vocab, er_vocab_pairs, idx, output_dim):
        batch = er_vocab_pairs[idx:idx + self.args.batch_size]
        targets = np.zeros((len(batch), output_dim))
        for idx, pair in enumerate(batch):
            if isinstance(pair,
                          np.ndarray):  # A workaround as test triples in kvold is a numpy array and a numpy array is not hashanle.
                pair = tuple(pair)
            targets[idx, er_vocab[pair]] = 1
        return np.array(batch), torch.FloatTensor(targets)

    def training(self):
        """
        Train models with KvsAll or NegativeSampling
        :return:
        """
        # 1. Select model and labelling : Entity Prediction or Relation Prediction.
        model, form_of_labelling = select_model(self.args)
        self.logger.info(f' Standard training starts: {model.name}-labeling:{form_of_labelling}')
        # 2. Create training data.
        dataset = StandardDataModule(train_set_idx=self.dataset.train_set_idx,
                                     valid_set_idx=self.dataset.val_set_idx,
                                     test_set_idx=self.dataset.test_set_idx,
                                     entities_idx=self.dataset.entity_to_idx,
                                     relations_idx=self.dataset.relation_to_idx,
                                     form=form_of_labelling,
                                     batch_size=self.args.batch_size,
                                     num_workers=self.args.num_workers)
        # 3. Display the selected model's architecture.
        self.logger.info(model)
        # 5. Train model
        self.trainer.fit(model, train_dataloader=dataset.train_dataloader())
        # 6. Test model on validation and test sets if possible.
        if self.dataset.val_set_idx:
            self.evaluate_lp_k_vs_all(model, self.dataset.val_set_idx, 'Evaluation of Validation set via KvsALL',
                                      form_of_labelling)
        if self.dataset.test_set_idx:
            self.evaluate_lp_k_vs_all(model, self.dataset.test_set_idx, 'Evaluation of Test set via KvsALL',
                                      form_of_labelling)
        return model

    def training_negative_sampling(self) -> pl.LightningModule:
        """
        Train models with Negative Sampling
        """
        assert self.neg_ratio > 0
        trainer = pl.Trainer.from_argparse_args(self.args)
        model, _ = select_model(self.args)
        form_of_labelling = 'NegativeSampling'
        self.logger.info(f' Training starts: {model.name}-labeling:{form_of_labelling}')

        dataset = StandardDataModule(train_set_idx=self.dataset.train_set_idx,
                                     valid_set_idx=self.dataset.val_set_idx,
                                     test_set_idx=self.dataset.test_set_idx,
                                     entities_idx=self.dataset.entity_to_idx,
                                     relations_idx=self.dataset.relation_to_idx,
                                     form=form_of_labelling,
                                     neg_sample_ratio=self.neg_ratio,
                                     batch_size=self.args.batch_size,
                                     num_workers=self.args.num_workers)

        self.logger.info(model)
        trainer.fit(model, train_dataloader=dataset.train_dataloader())
        if self.dataset.val_set_idx:
            self.evaluate_lp(model, self.dataset.val_set_idx, 'Evaluation of Validation set')
        if self.dataset.test_set_idx:
            self.evaluate_lp(model, self.dataset.test_set_idx, 'Evaluation of Test set')
        return model

    def evaluate_lp_k_vs_all(self, model, triple_idx, info, form_of_labelling):
        model.eval()
        hits = []
        ranks = []
        self.logger.info(info)
        for i in range(10):
            hits.append([])

        if form_of_labelling == 'RelationPrediction':
            ee_vocab = self.dataset.get_ee_idx_vocab()

            for i in range(0, len(triple_idx), self.args.batch_size):
                data_batch, _ = self.get_batch_1_to_N(ee_vocab, triple_idx, i, self.dataset.num_entities)
                e1_idx = torch.tensor(data_batch[:, 0])
                r_idx = torch.tensor(data_batch[:, 1])

                e2_idx = torch.tensor(data_batch[:, 2])
                predictions = model.forward_k_vs_all(e1_idx=e1_idx, e2_idx=e2_idx)
                for j in range(data_batch.shape[0]):
                    filt = ee_vocab[(data_batch[j][0], data_batch[j][1])]
                    target_value = predictions[j, r_idx[j]].item()
                    predictions[j, filt] = 0.0
                    predictions[j, r_idx[j]] = target_value

                sort_values, sort_idxs = torch.sort(predictions, dim=1, descending=True)
                sort_idxs = sort_idxs.cpu().numpy()
                for j in range(data_batch.shape[0]):
                    rank = np.where(sort_idxs[j] == r_idx[j].item())[0][0]
                    ranks.append(rank + 1)
                    for hits_level in range(10):
                        if rank <= hits_level:
                            hits[hits_level].append(1.0)
        else:
            er_vocab = self.dataset.get_er_idx_vocab()

            for i in range(0, len(triple_idx), self.args.batch_size):
                data_batch, _ = self.get_batch_1_to_N(er_vocab, triple_idx, i, self.dataset.num_relations)
                e1_idx = torch.tensor(data_batch[:, 0])
                r_idx = torch.tensor(data_batch[:, 1])
                e2_idx = torch.tensor(data_batch[:, 2])
                predictions = model.forward_k_vs_all(e1_idx=e1_idx, rel_idx=r_idx)
                for j in range(data_batch.shape[0]):
                    filt = er_vocab[(data_batch[j][0], data_batch[j][1])]
                    target_value = predictions[j, e2_idx[j]].item()
                    predictions[j, filt] = 0.0
                    predictions[j, e2_idx[j]] = target_value

                sort_values, sort_idxs = torch.sort(predictions, dim=1, descending=True)
                sort_idxs = sort_idxs.cpu().numpy()
                for j in range(data_batch.shape[0]):
                    rank = np.where(sort_idxs[j] == e2_idx[j].item())[0][0]
                    ranks.append(rank + 1)
                    for hits_level in range(10):
                        if rank <= hits_level:
                            hits[hits_level].append(1.0)

        hit_1 = sum(hits[0]) / (float(len(triple_idx)))
        hit_3 = sum(hits[2]) / (float(len(triple_idx)))
        hit_10 = sum(hits[9]) / (float(len(triple_idx)))
        mean_reciprocal_rank = np.mean(1. / np.array(ranks))

        results = {'H@1': hit_1, 'H@3': hit_3, 'H@10': hit_10, 'MRR': mean_reciprocal_rank}
        self.logger.info(results)
        return results

    def evaluate_lp(self, model, triple_idx, info):
        model.eval()
        self.logger.info(info)
        hits = dict()
        reciprocal_ranks = []
        er_vocab = self.dataset.get_er_idx_vocab()
        po_vocab = self.dataset.get_po_idx_vocab()

        for i in range(0, len(triple_idx)):
            # 1. Get a triple
            data_point = triple_idx[i]
            s, p, o = data_point[0], data_point[1], data_point[2]

            all_entities = torch.arange(0, self.dataset.num_entities).long()
            all_entities = all_entities.reshape(len(all_entities), )

            # 2. Predict missing heads and tails
            predictions_tails = model.forward_triples(e1_idx=torch.tensor(s).repeat(self.dataset.num_entities, ),
                                                      rel_idx=torch.tensor(p).repeat(self.dataset.num_entities, ),
                                                      e2_idx=all_entities)

            predictions_heads = model.forward_triples(e1_idx=all_entities,
                                                      rel_idx=torch.tensor(p).repeat(self.dataset.num_entities, ),
                                                      e2_idx=torch.tensor(o).repeat(self.dataset.num_entities))

            # 3. Computed filtered ranks.

            # 3.1. Compute filtered tail entity rankings
            filt_tails = er_vocab[(s, p)]

            target_value = predictions_tails[o].item()
            predictions_tails[filt_tails] = -np.Inf
            predictions_tails[o] = target_value
            _, sort_idxs = torch.sort(predictions_tails, descending=True)
            sort_idxs = sort_idxs.cpu().numpy()
            filt_tail_entity_rank = np.where(sort_idxs == o)[0][0]

            # 3.1. Compute filtered head entity rankings
            filt_heads = po_vocab[(p, o)]

            target_value = predictions_heads[s].item()
            predictions_heads[filt_heads] = -np.Inf
            predictions_heads[s] = target_value
            _, sort_idxs = torch.sort(predictions_heads, descending=True)
            sort_idxs = sort_idxs.cpu().numpy()
            filt_head_entity_rank = np.where(sort_idxs == s)[0][0]

            # 4. Add 1 to ranks as numpy array first item has the index of 0.
            filt_head_entity_rank += 1
            filt_tail_entity_rank += 1
            # 5. Store reciprocal ranks.
            reciprocal_ranks.append(1.0 / filt_head_entity_rank + (1.0 / filt_tail_entity_rank))

            # 4. Compute Hit@N
            for hits_level in range(1, 11):
                I = 1 if filt_head_entity_rank <= hits_level else 0
                I += 1 if filt_tail_entity_rank <= hits_level else 0
                if I > 0:
                    hits.setdefault(hits_level, []).append(I)

        mean_reciprocal_rank = sum(reciprocal_ranks) / (float(len(triple_idx) * 2))
        hit_1 = sum(hits[1]) / (float(len(triple_idx) * 2))
        hit_3 = sum(hits[3]) / (float(len(triple_idx) * 2))
        hit_10 = sum(hits[10]) / (float(len(triple_idx) * 2))

        results = {'H@1': hit_1, 'H@3': hit_3, 'H@10': hit_10,
                   'MRR': mean_reciprocal_rank}
        self.logger.info(results)
        return results

    def k_fold_cross_validation(self) -> pl.LightningModule:
        """
        Perform K-fold Cross-Validation

        1. Obtain K train and test splits.
        2. For each split,
            2.1 initialize trainer and model
            2.2. Train model with configuration provided in args.
            2.3. Compute the mean reciprocal rank (MRR) score of the model on the test respective split.
        3. Report the mean and average MRR .

        :param self:
        :return: model
        """
        self.logger.info(f'{self.args.num_folds_for_cv}-fold cross-validation starts.')
        kf = KFold(n_splits=self.args.num_folds_for_cv, shuffle=True)
        train_set_idx = np.array(self.dataset.train_set_idx)
        model = None
        eval_folds = []

        for (ith, (train_index, test_index)) in enumerate(kf.split(train_set_idx)):
            trainer = pl.Trainer.from_argparse_args(self.args)
            model, form_of_labelling = select_model(self.args)
            self.logger.info(
                f'{ith}-fold cross-validation starts: {model.name}-labeling:{form_of_labelling}, scoring technique: {self.scoring_technique}')

            train_set_for_i_th_fold, test_set_for_i_th_fold = train_set_idx[train_index], train_set_idx[test_index]

            dataset = StandardDataModule(train_set_idx=self.dataset.train_set_idx,
                                         valid_set_idx=self.dataset.val_set_idx,
                                         test_set_idx=self.dataset.test_set_idx,
                                         entities_idx=self.dataset.entity_to_idx,
                                         relations_idx=self.dataset.relation_to_idx,
                                         form=form_of_labelling,
                                         batch_size=self.args.batch_size,
                                         num_workers=self.args.num_workers)
            # 5. Train model
            trainer.fit(model, train_dataloader=dataset.train_dataloader())

            if self.scoring_technique == 'KvsAll' or model.name == 'Shallom':
                res = self.evaluate_lp_k_vs_all(model, test_set_for_i_th_fold,
                                                f'Evaluation at {ith}.th fold via KvsALL', form_of_labelling)
            elif self.scoring_technique == 'NegSample':
                res = self.evaluate_lp(model, test_set_for_i_th_fold, f'Evaluation at {ith}.th fold')
            eval_folds.append([res['MRR'], res['H@1'], res['H@3'], res['H@10']])

        eval_folds = pd.DataFrame(eval_folds, columns=['MRR', 'Hits@1', 'Hits@3', 'Hits@10'])
        # We may want to store it.
        self.logger.info(eval_folds.describe())

        self.logger.info('Model trained on last fold will be saved.')
        # Return last model.
        return model