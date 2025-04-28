import os
from datetime import datetime
from io import StringIO

import boto3
import pandas as pd

import Generic_Functions

# AWS S3 credentials
aws_access_key_id = os.environ['AWS_Public']
aws_secret_access_key = os.environ['AWS_Private']

# Create an S3 client
s3_client = boto3.client('s3',
                         aws_access_key_id=aws_access_key_id,
                         aws_secret_access_key=aws_secret_access_key)

#Construct filename parameters based on current date
current_month = datetime.now().strftime('%m')
last_month = (datetime.now() - pd.DateOffset(months=2)).strftime('%m')
current_year = datetime.now().strftime('%Y')
DateStamp = current_year + '/' + current_month + '/' + current_year + current_month
last_day = str(
    Generic_Functions.days_in_month(int(current_year), int(current_month)))
DateStamp_lastmonth = current_year + '/' + last_month + '/' + current_year + last_month\
  + str(Generic_Functions.days_in_month(int(current_year), int(last_month)))

# S3 bucket and file details
bucket_name = 'produk-rdsextracts-438255373632'
Account_Balance_file = DateStamp + '-accountbalance-TBUK.csv'
Accounts_file = DateStamp + '-Accounts-TBUK.csv'
RiskReport_file = DateStamp + '-RiskReport-TBUK.csv'
BookingData_file = DateStamp + '-BookingData-TBUK.csv'
BookingDataAll_file = DateStamp_lastmonth + '-BookingDataAll-TBUK.csv'


def Get_Account_Balance():
    # Read data into pandas DataFrame
    Account_Balance_object = s3_client.get_object(Bucket=bucket_name,
                                                  Key=Account_Balance_file)
    Account_Balance = pd.read_csv(
        StringIO(Account_Balance_object['Body'].read().decode('utf-8')))

    return Account_Balance['AccountBalance'].sum()


def Get_Accounts_Pipeline():
    # Read data into pandas DataFrame
    print(Account_Balance_file)
    Account_Balance_object = s3_client.get_object(Bucket=bucket_name,
                                                  Key=Account_Balance_file)
    Account_Balance = pd.read_csv(StringIO(
        Account_Balance_object['Body'].read().decode('utf-8')),
                                  index_col=0)
    #Drop unused columns
    Account_Balance = Account_Balance.drop(
        columns=['AccountCreationDate', 'DGRStatus'])

    print(Accounts_file)
    Accounts_object = s3_client.get_object(Bucket=bucket_name,
                                           Key=Accounts_file)
    Accounts = pd.read_csv(StringIO(
        Accounts_object['Body'].read().decode('utf-8')),
                           index_col=0)
    
    #print Account_Ballance Column Headings
    print(Accounts.columns)
    
    #Drop unused columns
    Accounts = Accounts.drop(columns=[
        'AccountName', 'BankAccountIsDeleted', 'BankAccountName', 'BSB',
        'AccNumber', 'BankAccountStatusId', 'Industry', 'SubIndustry',
        'GatewayGroup', 'DGRStatus', 'TierSystem', 'PreviousTier',
        'YearsLoyalty', 'TicketQuantity', 'AccountRating', 'PreviousRating',
        'RatingChanged', 'SchoolRecNumber', 'PromoCode'
    ])

    #Merge Accounts and Account_Balance on Index
    Accounts = Accounts.merge(Account_Balance,
                        left_index=True,
                        right_index=True)
    
    print(RiskReport_file)
    RiskReport_object = s3_client.get_object(Bucket=bucket_name,
                                             Key=RiskReport_file)
    RiskReport = pd.read_csv(StringIO(
        RiskReport_object['Body'].read().decode('utf-8')),
                             index_col=0)
    #Drop unused columns
    RiskReport = RiskReport.drop(columns=['AccountName'])

    #Merge Accounts and RiskReport
    Account_Pipeline = Accounts.merge(RiskReport,
                                      left_index=True,
                                      right_index=True)

    #Calculate and add Balance to Transfer Column
    Account_Pipeline['Balance to Transfer'] = Account_Pipeline.apply(
        lambda row: max(0, row['Balance'] - row['SalesForUpcomingEvents']),
        axis=1)

    print(Account_Pipeline.to_csv("Accounts_Pipeline.csv", index=True))

    return Account_Pipeline


def Get_Account_Historic_Revenue():
    # Read data into pandas DataFrame
    print(BookingDataAll_file)
    BookingDataAll_object = s3_client.get_object(Bucket=bucket_name,
                                                 Key=BookingDataAll_file)
    BookingDataAll = pd.read_csv(
        StringIO(BookingDataAll_object['Body'].read().decode('utf-8')))

    # Extract year from 'EventDate' column and create a new 'Year' column
    BookingDataAll['EventYear'] = pd.to_datetime(
        BookingDataAll['EventDate']).dt.year

    # Group by 'AccountID' and 'EventYear' and sum 'Payment Received'
    Historic_Account_Revenue = BookingDataAll.groupby(['AccountId', 'EventYear'])\
      ['PaymentReceived'].sum().reset_index()

    Historic_Account_Revenue.to_csv("Historic_Account_Revenue.csv",
                                    index=False)

    return "OK"


def Get_Event_Revenue():
    # Specify data types for columns
    column_data_types = {
        'BookingId': 'int64',
        'BookingTransactionId': 'int64',
        'AccountId': 'int64',
        'AccountName': 'object',
        'DateTimeCreated': 'datetime64',
        'EventId': 'int64',
        'EventName': 'object',
        'DonationCampaignId': 'float64',
        'DonationCampaignName': 'object',
        'TransactionDate': 'datetime64',
        'BookingUrlId': 'object',
        'PaymentReceived': 'float64',
        'TicketQuantity': 'int64',
        'BookingFee': 'float64',
        'CardFee': 'float64',
        'ProcessingFee': 'float64',
        'Surcharge': 'float64',
        'ProcessingFeeSurcharge': 'float64',
        'TicketFee': 'float64',
        'TransactionType': 'object',
        'PaymentType': 'object',
        'EventPostcode': 'object',
        'AccountPostcode': 'object',
        'EventDate': 'datetime64',
        'Industry': 'object',
        'SubIndustry': 'object',
        'GatewayGroup': 'object',
        'DGRStatus': 'object',
        'CustomerId': 'int64',
        'IPCountry': 'object',
        'Status': 'object',
        'Wallet': 'object',
        'GatewayName': 'object',
        'GatewayId': 'object',
        'GatewayReference': 'float64',
        'GiftCertificateTypeName': 'object',
        'GiftCertificateId': 'float64',
        'BookingCountryCode': 'object'
    }

    # Read data into pandas DataFrame
    print(BookingDataAll_file)
    BookingDataAll_object = s3_client.get_object(Bucket=bucket_name,
                                                 Key=BookingDataAll_file)
    
    BookingDataAll_data = BookingDataAll_object['Body'].read().decode('utf-8')
    BookingDataAll = pd.read_csv(StringIO(BookingDataAll_data), low_memory=False)
    
    print(BookingData_file)
    BookingData_object = s3_client.get_object(Bucket=bucket_name,
                                              Key=BookingData_file)
    BookingData = pd.read_csv(
        StringIO(BookingData_object['Body'].read().decode('utf-8')))

    #Merge BookingData and BookingDataAll
    BookingData = BookingData.merge(BookingDataAll,
                                    left_index=True,
                                    right_index=True)

    # Fill missing values with a placeholder value (e.g., 'Unknown' or '-')
    BookingData = BookingData.fillna('Unknown')

    # Drop unused columns
    #BookingData = BookingData.drop(columns=[
    #'BookingId', 'BookingTransactionId', 'AccountName', 'DateTimeCreated',
    #'BookingUrlId', 'AccountPostcode', 'Industry', 'SubIndustry',
    #'DGRStatus', 'CustomerId', 'IPCountry', 'GatewayId', 'GatewayReference',
    #'BookingCountryCode'
    #])

    # Group by fields and sum PaymentReceived and SalesForUpcomingEvents
    Event_Revenue = BookingData.groupby([
        'AccountId', 'EventId', 'EventName', 'EventDate', 'TransactionType',
        'PaymentType', 'GatewayGroup', 'Status', 'Wallet', 'GatewayName'
    ])[[
        'PaymentReceived', 'TicketQuantity', 'BookingFee', 'CardFee',
        'ProcessingFee', 'Surcharge', 'ProcessingFeeSurcharge', 'TicketFee'
    ]].sum().reset_index()

    Event_Revenue.to_csv("Event_Revenue.csv", index=False)

    return "OK"

    #open local csv file in directory c:\Downloads
    #with open('C:\Downloads\Event_Revenue.csv', 'r') as file: