from sklearn.decomposition import PCA
from sklearn.naive_bayes import GaussianNB
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import Imputer

from war.core import Strategy


class NaiveBayes(Strategy):

    def __init__(self):
        super().__init__(name='Naive Bayes',
                         max_parallel_tasks=1,
                         max_threads_per_estimator=1,
                         max_tasks=1)

    def next(self, nthreads):
        assert nthreads == 1
        model = make_pipeline(
            Imputer(),
            GaussianNB())
        return self.make_task(model, dict())


class PCANaiveBayes(Strategy):

    def __init__(self):
        super().__init__(name='PCA + Naive Bayes',
                         max_parallel_tasks=-1,
                         max_threads_per_estimator=1)
        self.nfeatures = None
        self.curr = None

    def init(self, info):
        self.nfeatures = info['features'].shape[1]
        self.curr = 1

    def next(self, nthreads):
        assert nthreads == 1
        while self.curr < self.nfeatures:
            model = make_pipeline(
                Imputer(),
                PCA(n_components=self.curr),
                GaussianNB())
            self.curr += 1
            task = self.make_task(model, dict())
            if task:
                return task
        raise StopIteration
