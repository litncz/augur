#SPDX-License-Identifier: MIT

import datetime
import json
import logging
import os
import sys
import warnings
from multiprocessing import Process, Queue
from workers.worker_git_integration import WorkerGitInterfaceable

import numpy as np
import pandas as pd
import requests
import sqlalchemy as s
from skimage.filters import threshold_otsu
from sklearn.ensemble import IsolationForest

from augur import ROOT_AUGUR_DIRECTORY
from workers.message_insights_worker.message_novelty import novelty_analysis
from workers.message_insights_worker.message_sentiment import get_senti_score
from workers.worker_base import Worker

warnings.filterwarnings('ignore')

class MessageInsightsWorker(WorkerGitInterfaceable):
    def __init__(self, config={}):

        # Define the worker's type, which will be used for self identification.
        worker_type = "message_insights_worker"

        # Define what this worker can be given and know how to interpret
        given = [['github_url']]

        # The name the housekeeper/broker use to distinguish the data model this worker can fill
        models = ['message_analysis']

        # Define the tables needed to insert, update, or delete on
        data_tables = ['message', 'repo', 'message_analysis', 'message_analysis_summary']

        # For most workers you will only need the worker_history and worker_job tables
        #   from the operations schema, these tables are to log worker task histories
        operations_tables = ['worker_history', 'worker_job']

        # Run the general worker initialization
        super().__init__(worker_type, config, given, models, data_tables, operations_tables)

        # Do any additional configuration after the general initialization has been run
        self.config.update(config)

        # Define data collection info
        self.tool_source = 'Message Insights Worker'
        self.tool_version = '0.0.2'
        self.data_source = 'Non-existent API'

        self.insight_days = self.config['insight_days']
        
        # Abs paths
        self.models_dir = os.path.join(ROOT_AUGUR_DIRECTORY, "workers", "message_insights_worker", self.config['models_dir'])
        self.full_train = False

        # To identify which run of worker inserted the data
        self.run_id = 100
        
    def message_analysis_model(self, task, repo_id):

        """ 
            :param task: the task generated by the housekeeper and sent to the broker which 
            was then sent to this worker. Takes the example dict format of:
                {
                    'job_type': 'MAINTAIN', 
                    'models': ['fake_data'], 
                    'display_name': 'fake_data model for url: https://github.com/vmware/vivace',
                    'given': {
                        'git_url': 'https://github.com/vmware/vivace'
                    }
                }
            :param repo_id: the collect() method queries the repo_id given the git/github url
            and passes it along to make things easier. An int such as: 27869
        """

        # Any initial database instructions, like finding the last tuple inserted or generate the next ID value   
        self.begin_date = ''

        # Check to see if repo has been analyzed previously
        repo_exists_SQL = s.sql.text("""
            SELECT exists (SELECT 1 FROM augur_data.message_analysis_summary WHERE repo_id = :repo_id LIMIT 1)""")
        
        df_rep = pd.read_sql_query(repo_exists_SQL, self.db, params={'repo_id': repo_id})
        self.full_train = not(df_rep['exists'].iloc[0])
        self.logger.info(f'Full Train: {self.full_train}')

        # Collection and insertion of data happens here

        if not self.full_train:
            
            # Fetch the timestamp of last analyzed message for the repo
            past_SQL = s.sql.text("""
                select message_analysis.msg_id, message.msg_timestamp
                from augur_data.message_analysis 
                inner join augur_data.message on message.msg_id = message_analysis.msg_id
                inner join augur_data.pull_request_message_ref on message.msg_id = pull_request_message_ref.msg_id 
                inner join augur_data.pull_requests on pull_request_message_ref.pull_request_id = pull_requests.pull_request_id
                where message.repo_id = :repo_id
                UNION
                select message_analysis.msg_id, message.msg_timestamp
                from augur_data.message_analysis
                inner join augur_data.message on message.msg_id = message_analysis.msg_id
                inner join augur_data.issue_message_ref on message.msg_id = issue_message_ref.msg_id 
                inner join augur_data.issues on issue_message_ref.issue_id = issues.issue_id
                where message.repo_id = :repo_id
                """)

            df_past = pd.read_sql_query(past_SQL, self.db, params={'repo_id': repo_id})
            df_past['msg_timestamp'] = pd.to_datetime(df_past['msg_timestamp'])
            df_past = df_past.sort_values(by='msg_timestamp')
            self.begin_date = df_past['msg_timestamp'].iloc[-1]

            # Assign new run_id for every run
            self.run_id = self.get_max_id('message_analysis', 'worker_run_id')
            
            self.logger.info(f'Last analyzed msg_id of repo {repo_id} is {df_past["msg_id"].iloc[-1]}')
            self.logger.info(f'Fetching recent messages from {self.begin_date} of repo {repo_id}...\n')
        
            # Fetch only recent messages
            join_SQL = s.sql.text("""
                select message.msg_id, msg_timestamp,  msg_text from augur_data.message
                left outer join augur_data.pull_request_message_ref on message.msg_id = pull_request_message_ref.msg_id 
                left outer join augur_data.pull_requests on pull_request_message_ref.pull_request_id = pull_requests.pull_request_id
                where message.repo_id = :repo_id and msg_timestamp > :begin_date
                UNION
                select message.msg_id, msg_timestamp, msg_text from augur_data.message
                left outer join augur_data.issue_message_ref on message.msg_id = issue_message_ref.msg_id 
                left outer join augur_data.issues on issue_message_ref.issue_id = issues.issue_id
                where message.repo_id = :repo_id and msg_timestamp > :begin_date""")
        else:
            self.logger.info(f'Fetching all past messages of repo {repo_id}...')

            # Fetch all messages
            join_SQL = s.sql.text("""
                select message.msg_id, msg_timestamp,  msg_text from augur_data.message
                left outer join augur_data.pull_request_message_ref on message.msg_id = pull_request_message_ref.msg_id 
                left outer join augur_data.pull_requests on pull_request_message_ref.pull_request_id = pull_requests.pull_request_id
                where message.repo_id = :repo_id
                UNION
                select message.msg_id, msg_timestamp, msg_text from augur_data.message
                left outer join augur_data.issue_message_ref on message.msg_id = issue_message_ref.msg_id 
                left outer join augur_data.issues on issue_message_ref.issue_id = issues.issue_id
                where message.repo_id = :repo_id""")

        df_message = pd.read_sql_query(join_SQL, self.db, params={'repo_id': repo_id, 'begin_date': self.begin_date})

        self.logger.info(f'Messages dataframe dim: {df_message.shape}')
        self.logger.info(f'Value 1: {df_message.shape[0]}')

        
        if df_message.shape[0] > 10:
            # Sort the dataframe
            df_message['msg_timestamp'] = pd.to_datetime(df_message['msg_timestamp'])
            df_message = df_message.sort_values(by='msg_timestamp')

            # DEBUG:
            # df_message.to_csv(f'full_{repo_id}.csv',index=False)
            
            # Create the storage directory for trained models
            try: 
                os.makedirs(self.models_dir, exist_ok=True)
                self.logger.info(f"Models storage directory is '{self.models_dir}'\n") 
            except OSError: 
                self.logger.error('Models storage directory could not be created \n')

            self.logger.info('Starting novelty detection...')
            threshold, df_message['rec_err'] = novelty_analysis(df_message,repo_id, self.models_dir, self.full_train, logger=self.logger)
            
            if not self.full_train:
                merge_SQL = s.sql.text("""
                select novelty_flag, reconstruction_error from augur_data.message_analysis
                left outer join augur_data.pull_request_message_ref on message_analysis.msg_id = pull_request_message_ref.msg_id 
                left outer join augur_data.pull_requests on pull_request_message_ref.pull_request_id = pull_requests.pull_request_id
                where pull_request_message_ref.repo_id = :repo_id
                UNION
                select novelty_flag, reconstruction_error from augur_data.message_analysis
                left outer join augur_data.issue_message_ref on message_analysis.msg_id = issue_message_ref.msg_id 
                left outer join augur_data.issues on issue_message_ref.issue_id = issues.issue_id
                where issue_message_ref.repo_id = :repo_id""")

                df_past = pd.read_sql_query(merge_SQL, self.db, params={'repo_id': repo_id})
                df_past = df_past.loc[df_past['novelty_flag'] == 0]
                rec_errors = df_past['reconstruction_error'].tolist()
                threshold = threshold_otsu(np.array(rec_errors))
            
            # Reset begin date for insight detection
            self.begin_date = datetime.datetime.combine(df_message['msg_timestamp'].iloc[-1] - datetime.timedelta(days=self.insight_days), datetime.time.min)
            self.logger.info(f'Begin date: {self.begin_date}\n')

            # Flagging novel messages
            self.logger.info(f'Threshold for novelty is: {threshold}')
            df_message['novel_label'] = df_message.apply(lambda x : 1 if x['rec_err'] > threshold and len(x['cleaned_msg_text'])>10 else 0, axis=1)
            self.logger.info(f'Novel messages detection completed. Dataframe dim: {df_message.shape}\n')

            self.logger.info('Starting sentiment analysis...')
            df_message['senti_label'], df_message['sentiment_score'] = get_senti_score(df_message, 'msg_text', self.models_dir, label=True, logger=self.logger)
            self.logger.info(f'Sentiment analysis completed. Dataframe dim: {df_message.shape}\n')

            df_message = df_message[['msg_id', 'msg_timestamp', 'sentiment_score', 'rec_err', 'senti_label', 'novel_label']]
            
            # DEBUG:
            # df_message.to_csv(f'{self.models_dir}/{repo_id}.csv',index=False)

            # Insertion of sentiment & uniqueness scores to message_analysis table
            self.logger.info('Begin message_analysis data insertion...')
            self.logger.info(f'{df_message.shape[0]} data records to be inserted')
            for row in df_message.itertuples(index=False):
                try:
                    msg = {
                        "msg_id": row.msg_id,
                        "worker_run_id": self.run_id,
                        "sentiment_score": row.sentiment_score,
                        "reconstruction_error": row.rec_err,
                        "novelty_flag": row.novel_label,
                        "feedback_flag": None,
                        "tool_source": self.tool_source,
                        "tool_version": self.tool_version,
                        "data_source": self.data_source,
                    }

                    result = self.db.execute(self.message_analysis_table.insert().values(msg))
                    self.logger.info(f'Primary key inserted into the message_analysis table: {result.inserted_primary_key}')
                    self.results_counter += 1
                    self.logger.info(f'Inserted data point {self.results_counter} with msg_id {row.msg_id} and timestamp {row.msg_timestamp}')
                except Exception as e:
                    self.logger.error(f'Error occurred while storing datapoint {repr(e)}')
                    break

            self.logger.info('Data insertion completed\n')           

            df_trend = df_message.copy()
            df_trend['date'] =  pd.to_datetime(df_trend["msg_timestamp"])
            df_trend = df_trend.drop(['msg_timestamp'],axis=1)
            df_trend = df_trend.sort_values(by='date')

            self.logger.info('Started 1D groupings to get insights')
            # Fetch the insight values of most recent specified insight_days
            # grouping = pd.Grouper(key='date', freq='1D')
            # df_insight = df_trend.groupby(grouping)['senti_label']
            # df_insight = df_insight.value_counts()
            # df_insight = df_insight.unstack()

            df_insight = df_trend.groupby(pd.Grouper(key='date', freq='1D'))['senti_label'].apply(lambda x : x.value_counts()).unstack()

            # df_insight = df_trend.groupby(pd.Grouper(key='date', freq='1D'))['senti_label'].value_counts().unstack()
            df_insight = df_insight.fillna(0)
            df_insight['Total'] = df_insight.sum(axis=1)
            df_insight = df_insight[df_insight.index >= self.begin_date]
            df_insight = df_insight.rename(columns={-1:'Negative',0: 'Neutral', 1: 'Positive'})
            
            # sentiment insight
            sent_col = ['Positive', 'Negative', 'Neutral', 'Total']
            for col in sent_col:
                if col not in list(df_insight.columns.values):
                    self.logger.info(f'{col} not found in datframe. Adjusting...')
                    df_insight[col] = np.zeros(df_insight.shape[0])

            sentiment_insights = df_insight[list(df_insight.columns.values)].sum(axis=0).to_dict()

            df_insight = df_trend.groupby(pd.Grouper(key='date', freq='1D'))['novel_label'].apply(lambda x : x.value_counts()).unstack()

            # df_insight = df_trend.groupby(pd.Grouper(key='date', freq='1D'))['novel_label'].value_counts().unstack()
            df_insight = df_insight.fillna(0)
            df_insight = df_insight.rename(columns={0: 'Normal', 1: 'Novel'})
            df_insight = df_insight[df_insight.index >= self.begin_date]
            
            # novel insight
            novel_col = ['Normal', 'Novel']
            for col in novel_col:
                if col not in list(df_insight.columns.values):
                    self.logger.info(f'{col} not found in datframe. Adjusting...')
                    df_insight[col] = np.zeros(df_insight.shape[0])
            novelty_insights = df_insight[list(df_insight.columns.values)].sum(axis=0).to_dict()

            # Log the insights
            self.logger.info(f'Insights on most recent analysed period from {self.begin_date} among {sentiment_insights["Total"]} messages')
            self.logger.info(f'Detected {sentiment_insights["Positive"]} positive_sentiment & {sentiment_insights["Negative"]} negative_sentiment messages')
            self.logger.info(f'Positive sentiment: {sentiment_insights["Positive"]/sentiment_insights["Total"]*100:.2f}% & Negative sentiment: {sentiment_insights["Negative"]/sentiment_insights["Total"]*100:.2f}%')
            self.logger.info(f'Detected {novelty_insights["Novel"]} novel messages')
            self.logger.info(f'Novel messages: {novelty_insights["Novel"]/sentiment_insights["Total"]*100:.2f}%\n')

            # Prepare data for repo level insertion
            self.logger.info('Started 15D groupings for summary')

            # Collecting positive and negative sentiment ratios with total messages
            df_senti = df_trend.groupby(pd.Grouper(key='date', freq='15D'))['senti_label'].apply(lambda x : x.value_counts()).unstack()


            # df_senti = df_trend.groupby(pd.Grouper(key='date', freq='15D'))['senti_label'].value_counts().unstack()
            df_senti = df_senti.fillna(0)
            df_senti['Total'] = df_senti.sum(axis=1)
            df_senti = df_senti.rename(columns={-1:'Negative', 0: 'Neutral', 1: 'Positive'})
            for col in sent_col:
                if col not in list(df_senti.columns.values):
                    self.logger.info(f'{col} not found in dataframe. Adjusting...')
                    df_senti[col] = np.zeros(df_senti.shape[0])
            df_senti['PosR'] = df_senti['Positive']/df_senti['Total']
            df_senti['NegR'] = df_senti['Negative']/df_senti['Total']
            
            # Collecting novel messages
            df_uniq = df_trend.groupby(pd.Grouper(key='date', freq='15D'))['novel_label'].apply(lambda x : x.value_counts()).unstack()


            # df_uniq = df_trend.groupby(pd.Grouper(key='date', freq='15D'))['novel_label'].value_counts().unstack()
            df_uniq = df_uniq.fillna(0)
            df_uniq = df_uniq.rename(columns={0: 'Normal', 1: 'Novel'})
            for col in novel_col:
                if col not in list(df_uniq.columns.values):
                    self.logger.info(f'{col} not found in dataframe. Adjusting...')
                    df_uniq[col] = np.zeros(df_uniq.shape[0])
            
            # Merge the 2 dataframes
            df_trend = pd.merge(df_senti, df_uniq, how="inner", left_index=True, right_index=True)
            self.logger.info(f'Merge done. Dataframe dim: {df_trend.shape}')

            # DEBUG:
            # df_trend.to_csv('trend.csv')
            
            # Insertion of sentiment ratios & novel counts to repo level table
            self.logger.info('Begin repo wise insights insertion...')
            self.logger.info(f'{df_senti.shape[0]} data records to be inserted\n')
            for row in df_trend.itertuples():
                try:
                    msg = {
                        "repo_id": repo_id,
                        "worker_run_id": self.run_id,
                        "positive_ratio": row.PosR,
                        "negative_ratio": row.NegR,
                        "novel_count": row.Novel,
                        "period": row.Index,
                        "tool_source": self.tool_source,
                        "tool_version": self.tool_version,
                        "data_source": self.data_source
                    }

                    result = self.db.execute(self.message_analysis_summary_table.insert().values(msg))
                    self.logger.info(f'Primary key inserted into the message_analysis_summary table: {result.inserted_primary_key}')
                    self.results_counter += 1
                    self.logger.info(f'Inserted data point {self.results_counter} for insight_period {row.Index}')
                except Exception as e:
                    self.logger.error(f'Error occurred while storing datapoint {repr(e)}')
                    break
            
            self.logger.info('Data insertion completed\n') 

            df_past = self.get_table_values(cols=['period','positive_ratio','negative_ratio', 'novel_count'], tables=['message_analysis_summary'], where_clause=f'where repo_id={repo_id}')
            df1 = df_past.copy()

            # Calculate deviation from mean for all metrics
            mean_vals = df_past[['positive_ratio','negative_ratio', 'novel_count']].iloc[:-1].mean(axis=0)
            curr_pos, curr_neg, curr_novel = df_past[['positive_ratio','negative_ratio', 'novel_count']].iloc[-1]
            pos_shift = (curr_pos - mean_vals[0]) / mean_vals[0] * 100
            neg_shift = (curr_neg - mean_vals[1]) / mean_vals[1] * 100
            novel_shift = (curr_novel - mean_vals[2]) / mean_vals[2] * 100

            deviation_insights = {'Positive': pos_shift, 'Negative': neg_shift, 'Novel': novel_shift}

            # Log the insights
            self.logger.info(f'Deviation Insights for last analyzed period from {df_past["period"].iloc[-1]}')
            self.logger.info(f'Sentiment Insights- Positive: {pos_shift:.2f}% & Negative: {neg_shift:.2f}%')
            self.logger.info(f'Novelty Insights: {novel_shift:.2f}%\n')

            # Anomaly detection on sentiment ratio based on past trends
            features = ['positive_ratio','negative_ratio']
            self.logger.info('Starting Isolation Forest on sentiment trend ratios...')
            clf = IsolationForest(n_estimators=100, max_samples='auto', max_features=1.0, bootstrap=False, n_jobs=-1, random_state=42, verbose=0)
            clf.fit(df1[features])
            pred = clf.predict(df1[features])
            df1['anomaly'] = pred
            anomaly_df = df1.loc[df1['anomaly'] == -1]
            anomaly_df = anomaly_df[anomaly_df['period'] >= self.begin_date]
            senti_anomaly_timestamps = anomaly_df['period'].to_list()

            # Log insights
            self.logger.info(f'Found {anomaly_df.shape[0]} anomalies from {self.begin_date}')
            self.logger.info('Displaying recent anomaly periods (if any)..')
            try:
                for i in range(len(senti_anomaly_timestamps)):
                    senti_anomaly_timestamps[i] = senti_anomaly_timestamps[i].strftime("%Y-%m-%d %H:%M:%S")
                    self.logger.info(f'Anomaly period {i+1}: {senti_anomaly_timestamps[i]}')
                    
            except Exception as e:
                self.logger.warning(e)
                self.logger.warning('No Anomalies detected in recent period!\n')

            insights = [sentiment_insights, novelty_insights, deviation_insights, senti_anomaly_timestamps]
            self.logger.info(f'Insights dict: {insights}')

            # Send insights to Auggie
            self.send_insight(repo_id, insights)

        else:
            if df_message.empty:
                self.logger.warning('No new messages in tables to analyze!\n')
            else:
                self.logger.warning("Insufficient data to analyze")
        
        # Register this task as completed.
        #   This is a method of the worker class that is required to be called upon completion
        #   of any data collection model, this lets the broker know that this worker is ready
        #   for another task
        self.register_task_completion(task, repo_id, 'message_analysis')

    def send_insight(self, repo_id, insights):
        try:
            repoSQL = s.sql.text("""
                SELECT repo_git, rg_name 
                FROM repo, repo_groups
                WHERE repo_id = {}
            """.format(repo_id))

            repo = pd.read_sql(repoSQL, self.db, params={}).iloc[0]
            to_send = {
                    'message_insight': True,
                    'repo_git': repo['repo_git'],
                    'insight_begin_date': self.begin_date.strftime("%Y-%m-%d %H:%M:%S"), # date from when insights are calculated
                    'sentiment': insights[0], # sentiment insight dict
                    'novelty': insights[1], # novelty insight dict
                    'recent_deviation': insights[2], # recent deviation insight dict
                    'sentiment_anomaly_timestamps': insights[3] # anomaly timestamps list from sentiment trend (if any)
                }
            requests.post('https://ejmoq97307.execute-api.us-east-1.amazonaws.com/dev/insight-event', json=to_send)
            self.logger.info("Sent insights to Auggie!\n")
        
        except Exception as e:
            self.logger.info("Sending insight to Auggie failed: {}\n".format(e))
