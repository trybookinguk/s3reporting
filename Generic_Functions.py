import datetime


def days_in_month(year, month):
  """
  Inputs:
    year  - an integer between datetime.MINYEAR and datetime.MAXYEAR
            representing the year
    month - an integer between 1 and 12 representing the month

  Returns:
    The number of days in the input month.
  """

  #if month is december, we proceed to next year
  def month_december(month):
    if month == 12:
      return 1
    else:
      return month + 1

  #if month is december, we proceed to next year
  def year_december(year, month):
    if month == 12:
      return year + 1
    else:
      return year

  #verify if month/year is valid
  if (month < 1) or (month > 12):
    print("please enter a valid month")
    exit()
  elif (year < 2000) or (year > 9999):
    print("please enter a valid year between 1 - 9999")
    exit()
  else:
    #subtract current month from next month then get days
    return (
        datetime.date(year_december(year, month), month_december(month), 1) -
        datetime.date(year, month, 1)).days
